"""INV-7 (#140) state builders: critiques, stratified follow-ups, and approved rounds. Test-only.

Follow-up approvals are constructed here, never in production code: production has no follow-up
authorizer until INV-10 (#143), so production fails closed (decision C3).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from packages.investigation.hypotheses import (
    FollowUpTask,
    InvestigationPlanV2,
    Stratification,
    parse_plan,
)
from packages.investigation.replanning import FollowUpApprovalRequest
from packages.investigation.state import (
    CritiqueFinding,
    CritiqueRecord,
    FollowUpApproval,
    FollowUpRoundRecord,
    follow_up_digest,
)
from tests.investigation_churn_schema import churn_v2_plan, churn_v2_raw
from tests.investigation_fixtures import APPROVED_AT, apply_update, running_state

TENURE = Stratification(field="tenure_months_start", cut_points=(6,))
SMB_BASIC = [
    {"field": "customer_segment", "value": "SMB"},
    {"field": "plan_tier", "value": "Basic"},
]


def v2_running_state(**tasks: Any) -> dict[str, Any]:
    """A valid, running, approved state for the INV-6 churn v2 plan."""
    return running_state(plan=churn_v2_plan(), **tasks)


def stratified_follow_up(
    task_id: str = "h1_tenure",
    *,
    target: str = "h1_rate",
    hypothesis: str = "h1",
    filters: list[dict[str, str]] | None = None,
    stratification: Stratification = TENURE,
) -> FollowUpTask:
    """A stratified re-test of an approved churn rate test, as the Supervisor builds it."""
    return FollowUpTask.model_validate(
        {
            "id": task_id,
            "kind": "hypothesis_test",
            "assignee": "sql_analyst",
            "depends_on": [target],
            "success_criterion": "Counts per period and stratum.",
            "max_attempts": 1,
            "input": {
                "kind": "hypothesis_test",
                "hypothesis_id": hypothesis,
                "method": "stratified_comparison",
                "metric": {"type": "rate", "event": "churned", "population": "active_at_start"},
                "filters": SMB_BASIC if filters is None else filters,
                "period": {
                    "field": "quarter_start",
                    "baseline": "2026-01-01",
                    "comparison": "2026-04-01",
                },
                "claim": {"direction": "increase", "min_effect": 0.01},
                "stratification": stratification.model_dump(mode="json"),
            },
        }
    )


def gap_finding(
    task: str = "h1_rate", hypothesis: str = "h1", stratification: Stratification = TENURE
) -> CritiqueFinding:
    return CritiqueFinding(
        code="missing_stratification",
        severity="medium",
        disposition="follow_up",
        origin="model_confirmed",
        hypothesis_id=hypothesis,
        task_id=task,
        stratification=stratification,
    )


def critique(
    round_index: int = 0,
    *,
    findings: tuple[CritiqueFinding, ...] | None = None,
    replan: str = "admitted",
    verdicts: tuple[Any, ...] = (),
) -> dict[str, Any]:
    if findings is None:
        findings = (gap_finding(),) if replan != "none_needed" else ()
    record = CritiqueRecord.model_validate(
        {
            "round": round_index,
            "draft_status": "valid",
            "review_status": "valid",
            "findings": [item.model_dump(mode="json") for item in findings],
            "verdicts": [item for item in verdicts],
            "replan": replan,
        }
    )
    return dict(record.model_dump(mode="json"))


def approval_for(
    tasks: list[FollowUpTask], round_index: int, *, hours: float = 6.0
) -> FollowUpApproval:
    approved_at = datetime.fromisoformat(APPROVED_AT) + timedelta(minutes=30)
    return FollowUpApproval(
        follow_up_digest=follow_up_digest(tasks),
        round=round_index,
        approved_at=approved_at,
        deadline=approved_at + timedelta(hours=hours),
    )


def round_record(
    tasks: list[FollowUpTask],
    round_index: int = 1,
    *,
    finding_indexes: tuple[int, ...] | None = None,
    approval: FollowUpApproval | None = None,
) -> dict[str, Any]:
    record = FollowUpRoundRecord(
        round=round_index,
        critique_round=round_index - 1,
        task_ids=tuple(task.id for task in tasks),
        finding_indexes=finding_indexes or tuple(range(len(tasks))),
        follow_up_digest=follow_up_digest(tasks),
        approval=approval or approval_for(tasks, round_index),
    )
    return dict(record.model_dump(mode="json"))


def admission(tasks: list[FollowUpTask], round_index: int = 1, **kwargs: Any) -> dict[str, Any]:
    """The Supervisor's one guarded update that admits a round."""
    return {
        "admitted_follow_ups": [task.model_dump(mode="json") for task in tasks],
        "follow_up_rounds": [round_record(tasks, round_index, **kwargs)],
    }


def admitted_state(
    tasks: list[FollowUpTask] | None = None, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A running v2 state with critique 0 (an admitted replan) and follow-up round 1."""
    tasks = tasks or [stratified_follow_up()]
    state = state or v2_running_state()
    findings = tuple(
        gap_finding(task.depends_on[0], task.input.hypothesis_id, task.input.stratification)
        for task in tasks
    )
    state = apply_update(state, "critic", {"critiques": [critique(0, findings=findings)]})
    return apply_update(state, "supervisor", admission(tasks))


# --- scripted model output (written by the test author; production never reads it) -----------

_ACTIVE = "LOWER(CAST(active_at_start AS TEXT)) IN ('true', '1')"
_CHURNED = "SUM(CASE WHEN LOWER(CAST(churned AS TEXT)) IN ('true', '1') THEN 1 ELSE 0 END)"


def stratified_sql(field: str = "tenure_months_start", cut: int = 6) -> str:
    """Scripted SQL for an SMB Basic follow-up: band_0 below ``cut``, band_1 at or above it."""
    band = f"CASE WHEN CAST({field} AS INTEGER) < {cut} THEN 'band_0' ELSE 'band_1' END"
    return (
        f"SELECT quarter_start, customer_segment, plan_tier, {band} AS stratum, "
        f"{_CHURNED} AS numerator, COUNT(*) AS denominator FROM uploaded_data "
        f"WHERE {_ACTIVE} AND quarter_start IN ('2026-01-01', '2026-04-01') "
        "AND customer_segment = 'SMB' AND plan_tier = 'Basic' "
        "GROUP BY quarter_start, customer_segment, plan_tier, stratum "
        "ORDER BY quarter_start, stratum"
    )


def tenure_sql(cut: int = 6) -> str:
    """Scripted stratified SQL for the SMB Basic tenure follow-up."""
    return stratified_sql("tenure_months_start", cut)


# --- the INV-7 churn plan: the INV-6 plan plus one approved memo task -------------------------

MEMO_QUERY = "Enterprise churn statement in the draft board memo"


def churn_v7_raw() -> dict[str, Any]:
    """The INV-6 churn v2 plan with an approved ``memo`` document task as H4's context."""
    raw = churn_v2_raw()
    raw["tasks"].insert(
        4,
        {
            "id": "memo",
            "kind": "document_research",
            "assignee": "document_research",
            "input": {
                "kind": "document_research",
                "query": MEMO_QUERY,
                "purpose": "contradiction_check",
            },
            "success_criterion": "Cited memo passage.",
        },
    )
    for hypothesis in raw["hypotheses"]:
        if hypothesis["id"] == "h4":
            hypothesis["context_tasks"] = ["memo"]
    return raw


def churn_v7_plan() -> InvestigationPlanV2:
    plan = parse_plan(churn_v7_raw())
    assert isinstance(plan, InvestigationPlanV2)
    return plan


def churn_v7_response() -> str:
    return json.dumps({"plan": churn_v7_raw(), "missing_information": []}, separators=(",", ":"))


def gap(task: str = "h1_rate", field: str = "tenure_months_start", cut: int = 6) -> dict[str, Any]:
    hypothesis = task.split("_")[0]
    return {"hypothesis_id": hypothesis, "test_task_id": task, "field": field, "cut_points": [cut]}


def review(*gaps: dict[str, Any]) -> str:
    return json.dumps({"gaps": list(gaps)})


class ScriptedAuthorizer:
    """A test-only follow-up approver. ``mode``: approve, deny, raise, wrong_digest, wrong_round,
    expired, or outside_plan (deadline past the plan approval)."""

    def __init__(self, mode: str = "approve") -> None:
        self.mode = mode
        self.requests: list[FollowUpApprovalRequest] = []

    def authorize(self, request: FollowUpApprovalRequest) -> FollowUpApproval | None:
        self.requests.append(request)
        if self.mode == "deny":
            return None
        if self.mode == "raise":
            raise RuntimeError("approval service unavailable")
        tasks = list(request.tasks)
        approval = approval_for(tasks, request.round)
        if self.mode == "wrong_digest":
            return approval.model_copy(update={"follow_up_digest": "0" * 64})
        if self.mode == "wrong_round":
            return approval_for(tasks, 3 - request.round)
        if self.mode == "expired":
            return approval_for(tasks, request.round, hours=1)  # ends before the fixture clock
        if self.mode == "outside_plan":
            return approval_for(tasks, request.round, hours=48)
        return approval


def v7_execution(
    index: Any,
    *,
    drafts: list[str] | None = None,
    reviews: list[str] | None = None,
    authorizer: Any = None,
    sql: dict[str, str] | None = None,
    **kwargs: Any,
) -> Any:
    """An approved INV-7 churn execution on the real data and index, with scripted reviews."""
    from tests.investigation_execution_fakes import NO_CLAIMS, NO_GAPS, RoutedAdapter
    from tests.investigation_hypothesis_fakes import (
        CITED_ANSWER,
        HYPOTHESIS_SQL,
        v2_execution,
        v2_proposal,
    )

    routes = {
        "support_tickets < 1": stratified_sql("support_tickets", 1),
        "tenure_months_start < 6": tenure_sql(),
        **HYPOTHESIS_SQL,
        **(sql or {}),
    }
    llm = kwargs.pop("llm", None) or RoutedAdapter(
        document_answer=kwargs.pop("answer", CITED_ANSWER),
        sql=routes,
        drafts=drafts or [NO_CLAIMS],
        reviews=reviews or [NO_GAPS],
    )
    plan = churn_v7_plan()
    return v2_execution(
        index,
        plan=plan,
        proposal=v2_proposal(churn_v7_response()),
        llm=llm,
        authorizer=authorizer,
        **kwargs,
    )
