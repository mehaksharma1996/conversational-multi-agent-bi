"""The Document Research specialist (ADR 0024, issue #138 INV-5 S3).

Document Research runs one admitted ``document_research`` task through a ``SpecialistGateway``
and nothing else: ``begin_attempt -> research_documents``, one port call per ``step``. The query
and passage limit come from the approved plan inside the gateway; the attempt token stays in this
object. Retrieved passages and the model's answer never reach this module: the gateway keeps them
as process-local evidence. The specialist never retries, skips, or closes a task.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from packages.investigation import AgentRole
from src.orchestration.tool_gateway import (
    AttemptToken,
    BeginAttempt,
    GatewayResult,
    ResearchDocuments,
    SpecialistGateway,
)

Phase = Literal["begin", "research", "done"]


class DocumentResearcher:
    """One document task's operation sequence. Holds only the port's own attempt token."""

    role = AgentRole.DOCUMENT_RESEARCH

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.phase: Phase = "begin"
        self.token: AttemptToken | None = None

    @property
    def has_next(self) -> bool:
        return self.phase != "done"

    def new_attempt(self) -> None:
        """Start the sequence again for an attempt the dispatcher decided to begin."""
        self.phase = "begin"
        self.token = None

    def step(self, port: SpecialistGateway, state: Mapping[str, Any]) -> GatewayResult:
        """Make exactly one port call: the next operation of the current attempt."""
        if self.phase == "begin":
            result = port.begin_attempt(state, BeginAttempt(task_id=self.task_id, role=self.role))
            self.token = result.attempt
            self.phase = "research" if result.attempt is not None else "done"
        elif self.phase == "research":
            token, self.token = self.token, None
            if token is None:
                raise RuntimeError("no attempt token")
            result = port.research_documents(
                state, ResearchDocuments(task_id=self.task_id, role=self.role, attempt=token)
            )
            self.phase = "done"
        else:
            raise RuntimeError("the attempt has no further operation")
        return result
