"""Shared INV-8 (#141) fixtures: the real scripted churn run and helpers for report tests.

The churn run is the INV-7 acceptance run: the real ``subscription_churn.csv`` in SQLite, the real
four-PDF index, and the real owner, gateway, specialists, Critic, and Supervisor. It runs under
scripted bounded adapters:
- the first findings draft seeds the board memo's Enterprise claim, so the critique records a
  ``document_contradiction``;
- the Critic model asks for the tenure stratification;
- a test-only authorizer approves the one follow-up round.

The scripted narrative model (INV-8) answers with whatever ``narratives`` holds. A callable entry
receives the prompt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.llm.base import LLMResponse
from src.orchestration.review_prompts import FINDINGS_MARKER
from tests.investigation_critique_fakes import (
    ScriptedAuthorizer,
    gap,
    review,
    stratified_sql,
    v7_execution,
)
from tests.investigation_execution_fakes import RoutedAdapter, reset_replay_registry
from tests.investigation_hypothesis_fakes import CITED_ANSWER, HYPOTHESIS_SQL, churn_index

ROOT = Path(__file__).resolve().parents[1]
ORACLE = json.loads((ROOT / "evals" / "investigation" / "v1" / "oracle.json").read_text("utf-8"))
NARRATIVE_MARKER = "INVESTIGATION REPORT NARRATIVE"
_MEASUREMENT = re.compile(r"^- (\w+) \((h\d+)\): .*evidence (ev_[0-9a-f]{32})$", re.M)
_DOCUMENT = re.compile(r"^- (ev_[0-9a-f]{32}) \((\w+);", re.M)

Narrative = str | Callable[[str], str]


@dataclass(eq=False)
class ChurnReportAdapter(RoutedAdapter):
    """INV-7's scripted churn models, plus a scripted narrative model (INV-8)."""

    narratives: list[Narrative] = field(default_factory=lambda: ["{}"])
    narrative_prompts: list[str] = field(default_factory=list)
    seeded: bool = False

    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        if prompt.startswith(NARRATIVE_MARKER):
            self.narrative_prompts.append(prompt)
            answer = self.narratives[min(len(self.narrative_prompts), len(self.narratives)) - 1]
            self.text = answer(prompt) if callable(answer) else answer
            return super(RoutedAdapter, self).generate_once(
                prompt, max_output_tokens=max_output_tokens
            )
        if prompt.startswith(FINDINGS_MARKER) and not self.seeded:
            self.seeded = True
            documents = dict(_DOCUMENT.findall(prompt))
            memo = next(eid for eid, task in documents.items() if task == "memo")
            claim = {
                "hypothesis_id": "h4",
                "test_task_id": "h4_rate",
                "basis": "document",
                "direction": "increase",
                "evidence_ids": [memo],
            }
            self.drafts = [json.dumps({"claims": [claim]}), '{"claims":[]}']
        return super().generate_once(prompt, max_output_tokens=max_output_tokens)


def churn_adapter(narratives: list[Narrative] | None = None) -> ChurnReportAdapter:
    return ChurnReportAdapter(
        document_answer=CITED_ANSWER,
        sql={"tenure_months_start < 6": stratified_sql(), **HYPOTHESIS_SQL},
        reviews=[review(gap())],
        narratives=narratives or ["{}"],
    )


def churn_execution(
    directory: Path,
    *,
    narratives: list[Narrative] | None = None,
    authorizer: Any = None,
    **kwargs: Any,
) -> Any:
    """An approved churn execution (INV-7 plan), ready to ``run``."""
    reset_replay_registry()
    index = churn_index(directory)
    llm = kwargs.pop("llm", None) or churn_adapter(narratives)
    return v7_execution(
        index,
        llm=llm,
        authorizer=ScriptedAuthorizer() if authorizer is None else authorizer,
        **kwargs,
    )


def evidence_of(result: Any, kind: str, task_id: str) -> str:
    return next(
        eid
        for eid, record in result.state.evidence.items()
        if record.kind == kind and record.task_id == task_id
    )
