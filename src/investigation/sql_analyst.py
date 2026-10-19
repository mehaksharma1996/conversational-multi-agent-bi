"""The SQL Analyst specialist (ADR 0024, issue #138 INV-5 S3).

The SQL Analyst runs one admitted ``sql_analysis`` task through a ``SpecialistGateway`` and
nothing else. Each ``step`` makes exactly one port call, in a fixed sequence:

``begin_attempt -> prepare_sql -> execute_sql``, and, when that execution failed with the
gateway holding the task's one correction, ``prepare_correction -> execute_sql``.

The attempt token and SQL handle stay in this object; they are never written to state. The
specialist never retries, skips, or closes a task: after an attempt ends, the dispatcher decides
(``packages.investigation.dispatch``) and, for a retry, calls ``new_attempt``. The question, SQL,
and correction text come only from the gateway, which builds them from the approved plan.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from packages.investigation import AgentRole
from src.orchestration.tool_gateway import (
    AttemptToken,
    BeginAttempt,
    ExecuteSQL,
    GatewayResult,
    PrepareCorrection,
    PrepareSQL,
    SpecialistGateway,
    SQLHandle,
)

Phase = Literal["begin", "prepare", "execute", "correct", "done"]


def _correctable(result: GatewayResult) -> bool:
    """A failed execution the gateway left running: the content-free sign of a held correction."""
    decisions = result.updates.get("decisions") or []
    if result.refusal is not None or result.evidence_id is not None or not decisions:
        return False
    last = decisions[-1]
    return (
        last.get("tool") == "execute_sql"
        and last.get("result") == "failed"
        and last.get("category") == "sql_generation"
        and "tasks" not in result.updates  # the task did not end
    )


class SQLAnalyst:
    """One SQL task's operation sequence. Holds only the port's own tokens and handles."""

    role = AgentRole.SQL_ANALYST

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.phase: Phase = "begin"
        self.token: AttemptToken | None = None
        self.handle: SQLHandle | None = None
        self.correction_used = False  # the gateway counts one correction per task, not attempt

    @property
    def has_next(self) -> bool:
        """Whether the current attempt has another operation in its fixed sequence."""
        return self.phase != "done"

    def new_attempt(self) -> None:
        """Start the sequence again for an attempt the dispatcher decided to begin."""
        self.phase = "begin"
        self.token = None
        self.handle = None

    def step(self, port: SpecialistGateway, state: Mapping[str, Any]) -> GatewayResult:
        """Make exactly one port call: the next operation of the current attempt."""
        if self.phase == "begin":
            result = port.begin_attempt(state, BeginAttempt(task_id=self.task_id, role=self.role))
            self.token = result.attempt
            self.phase = "prepare" if result.attempt is not None else "done"
        elif self.phase == "prepare":
            token, self.token = self.token, None
            if token is None:
                raise RuntimeError("no attempt token")
            result = port.prepare_sql(
                state, PrepareSQL(task_id=self.task_id, role=self.role, attempt=token)
            )
            self.after_prepare(result)
        elif self.phase == "execute":
            handle, self.handle = self.handle, None
            if handle is None:
                raise RuntimeError("no SQL handle")
            result = port.execute_sql(
                state, ExecuteSQL(task_id=self.task_id, role=self.role, handle=handle)
            )
            correct = not self.correction_used and _correctable(result)
            self.phase = "correct" if correct else "done"
        elif self.phase == "correct":
            self.correction_used = True
            result = port.prepare_correction(
                state, PrepareCorrection(task_id=self.task_id, role=self.role)
            )
            self.after_prepare(result)
        else:
            raise RuntimeError("the attempt has no further operation")
        return result

    def after_prepare(self, result: GatewayResult) -> None:
        self.handle = result.handle
        self.phase = "execute" if result.handle is not None else "done"
