"""Real churn-schema builders for INV-6 tests (#139): planning metadata and v2 hypothesis plans.

The INV-4/INV-5 planning fakes use outdated column names (``segment``, ``tenure_months``). INV-6
plans against the real ``sample_data/subscription_churn.csv`` header instead.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from packages.investigation.hypotheses import InvestigationPlanV2, parse_plan
from packages.investigation.planning import (
    DocumentTitleMetadata,
    PlanningMetadata,
    SchemaFieldMetadata,
)
from packages.investigation.validation import SchemaContext

ROOT = Path(__file__).resolve().parents[1]
CHURN_CSV = ROOT / "sample_data" / "subscription_churn.csv"
with CHURN_CSV.open(encoding="utf-8") as _stream:
    CHURN_COLUMNS: tuple[str, ...] = tuple(next(csv.reader(_stream)))
_TYPES = {
    "tenure_months_start": "integer",
    "monthly_price": "number",
    "mrr_at_start": "number",
    "payment_failures_90d": "integer",
    "support_tickets": "integer",
    "avg_resolution_hours": "number",
    "active_days_last_30": "integer",
    "sessions_last_30": "integer",
    "active_at_start": "boolean",
    "renewal_due": "boolean",
    "active_at_end": "boolean",
    "churned": "boolean",
}
CHURN_TITLES = (
    "Pricing change notice",
    "Support operations update",
    "Retention playbook",
    "Draft board memo",
)
CHURN_QUESTION = (
    "Why did customer churn increase last quarter, which segments contributed most, and what "
    "should management do about it?"
)
CHURN_SCHEMA = SchemaContext.of(CHURN_COLUMNS)


def churn_metadata() -> PlanningMetadata:
    return PlanningMetadata(
        question=CHURN_QUESTION,
        schema_fields=tuple(
            SchemaFieldMetadata(name=name, logical_type=_TYPES.get(name, "text"), description=None)
            for name in CHURN_COLUMNS
        ),
        document_titles=tuple(DocumentTitleMetadata(title=title) for title in CHURN_TITLES),
    )


def _task(
    task_id: str, kind: str, assignee: str, task_input: dict[str, Any], *deps: str
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": task_id,
        "kind": kind,
        "assignee": assignee,
        "input": task_input,
        "success_criterion": "Counts per group and period.",
    }
    if deps:
        raw["depends_on"] = list(deps)
    return raw


def _filter(field: str, value: str) -> dict[str, str]:
    return {"field": field, "value": value}


def _test(
    hypothesis: str,
    filters: list[dict[str, str]],
    *,
    direction: str = "increase",
    min_effect: float = 0.01,
    metric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "kind": "hypothesis_test",
        "hypothesis_id": hypothesis,
        "method": "trend_comparison",
        "filters": filters,
        "claim": {"direction": direction, "min_effect": min_effect},
    }
    if metric is not None:
        spec["metric"] = metric
    return spec


def churn_v2_raw() -> dict[str, Any]:
    """The compact v2 churn plan for H1-H4, as a model would emit it (JSON-native)."""
    return {
        "schema_version": 2,
        "objective": "Explain the 2026-Q2 churn increase, its segments, and supported actions.",
        "sub_questions": [
            "How much did churn change?",
            "Which segments contributed most?",
            "Which hypotheses do the data and documents support?",
        ],
        "test_defaults": {
            "metric": {"type": "rate", "event": "churned", "population": "active_at_start"},
            "period": {
                "field": "quarter_start",
                "baseline": "2026-01-01",
                "comparison": "2026-04-01",
            },
        },
        "tasks": [
            _task(
                "trend",
                "sql_analysis",
                "sql_analyst",
                {
                    "kind": "sql_analysis",
                    "objective": "Churn rate per quarter.",
                    "fields": ["quarter_start", "active_at_start", "churned"],
                },
            ),
            _task(
                "pricing",
                "document_research",
                "document_research",
                {
                    "kind": "document_research",
                    "query": "Pricing change effective date",
                    "purpose": "policy",
                },
            ),
            _task(
                "support",
                "document_research",
                "document_research",
                {
                    "kind": "document_research",
                    "query": "Support backlog by region",
                    "purpose": "context",
                },
            ),
            _task(
                "playbook",
                "document_research",
                "document_research",
                {
                    "kind": "document_research",
                    "query": "Retention playbook records",
                    "purpose": "context",
                },
            ),
            _task(
                "h1_rate",
                "hypothesis_test",
                "sql_analyst",
                _test("h1", [_filter("customer_segment", "SMB"), _filter("plan_tier", "Basic")]),
            ),
            _task(
                "h2_rate",
                "hypothesis_test",
                "sql_analyst",
                _test("h2", [_filter("region", "APAC")]),
            ),
            _task(
                "h2_resolution",
                "hypothesis_test",
                "sql_analyst",
                _test(
                    "h2",
                    [_filter("region", "APAC")],
                    min_effect=1.0,
                    metric={"type": "mean", "value": "avg_resolution_hours"},
                ),
            ),
            _task(
                "h4_rate",
                "hypothesis_test",
                "sql_analyst",
                _test("h4", [_filter("customer_segment", "Enterprise")]),
            ),
        ],
        "hypotheses": [
            {
                "id": "h1",
                "statement": "SMB Basic churn rose contemporaneously with the pricing change.",
                "tests": ["h1_rate"],
                "context_tasks": ["pricing"],
                "context_required": True,
            },
            {
                "id": "h2",
                "statement": (
                    "APAC churn is associated with the support backlog and long resolutions."
                ),
                "tests": ["h2_rate", "h2_resolution"],
                "context_tasks": ["support"],
            },
            {
                "id": "h3",
                "statement": "The retention playbook reduced churn.",
                "testability": "untestable",
                "missing_data": ["Which customers received a retention intervention."],
                "context_tasks": ["playbook"],
            },
            {
                "id": "h4",
                "statement": "Enterprise churn increased in 2026-Q2.",
                "tests": ["h4_rate"],
            },
        ],
    }


def churn_v2_plan() -> InvestigationPlanV2:
    plan = parse_plan(churn_v2_raw())
    assert isinstance(plan, InvestigationPlanV2)
    return plan


def churn_v2_response() -> str:
    """The compact planning response text for the v2 churn plan."""
    return json.dumps({"plan": churn_v2_raw(), "missing_information": []}, separators=(",", ":"))
