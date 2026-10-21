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
from tests.test_investigation_sql_gateway import ScriptedSQL

ROOT = Path(__file__).resolve().parents[1]
ORACLE = json.loads((ROOT / "evals" / "investigation" / "v1" / "oracle.json").read_text("utf-8"))
NARRATIVE_MARKER = "INVESTIGATION REPORT NARRATIVE"
NARRATIVE_MAX_INPUT = 12_000
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
            # Past ``ScriptedSQL`` (as ``RoutedAdapter`` does), so the scripted text is returned.
            return super(ScriptedSQL, self).generate_once(
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
    # The fixtures bill one token per byte; the narrative prompt lists every fact and code.
    kwargs.setdefault("max_input", NARRATIVE_MAX_INPUT)
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


_HEADERS = {
    "Facts:": "facts",
    "Hypotheses:": "hypotheses",
    "Tasks:": "tasks",
    "Findings:": "findings",
    "Limitations and notes:": "limitations",
    "Recommendations:": "recommendations",
}


def prompt_items(prompt: str) -> dict[str, Any]:
    """The listed items of a narrative prompt, by heading, as a model would read them."""
    items: dict[str, Any] = {name: [] for name in _HEADERS.values()}
    current = None
    for line in prompt.splitlines():
        if line.startswith("Scope: "):
            items["scope"] = json.loads(line[len("Scope: ") :])
        elif line in _HEADERS:
            current = _HEADERS[line]
        elif current is not None and line.startswith("- "):
            items[current].append(json.loads(line[2:]))
    return items


def honest_narrative(prompt: str, **overrides: Any) -> str:
    """What a well-behaved narrative model returns: every listed item, as typed claims only.

    ``overrides`` replace whole sections' claim lists (to seed a violation).
    """
    items = prompt_items(prompt)
    scope = items["scope"]
    tested = [row for row in items["hypotheses"] if row["status"] == "tested"]
    sections = {
        "executive_summary": [
            {"type": "scope", **scope},
            *(
                {
                    "type": "verdict",
                    "hypothesis_id": row["hypothesis_id"],
                    "verdict": row["verdict"],
                }
                for row in tested
            ),
            {"type": "limitation", "code": "causation_not_established"},
        ],
        "findings": [
            {"type": "fact", "fact_id": fact["fact_id"], "direction": fact["direction"]}
            for fact in items["facts"]
        ],
        "hypotheses": [
            {"type": "verdict", "hypothesis_id": row["hypothesis_id"], "verdict": row["verdict"]}
            for row in items["hypotheses"]
            if row["verdict"] is not None
        ],
        "critique": [
            {
                "type": "finding",
                "round": item["round"],
                "index": item["index"],
                "code": item["code"],
            }
            for item in items["findings"]
        ],
        "limitations": [
            {
                "type": "limitation",
                "code": item["code"],
                "task_id": item.get("task_id"),
                "hypothesis_id": item.get("hypothesis_id"),
            }
            for item in items["limitations"]
        ],
        "recommendations": [
            {
                "type": "recommendation",
                "recommendation_id": item["recommendation_id"],
                "rule_id": item["rule_id"],
            }
            for item in items["recommendations"]
        ]
        or [{"type": "no_recommendation"}],
    }
    sections.update(overrides)
    return json.dumps(
        {"sections": [{"name": name, "claims": claims} for name, claims in sections.items()]}
    )
