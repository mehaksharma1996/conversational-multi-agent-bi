"""INV-7 (#140) state builders: critiques, stratified follow-ups, and approved rounds. Test-only.

Follow-up approvals are constructed here, never in production code: production has no follow-up
authorizer until INV-10 (#143), so production fails closed (decision C3).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from packages.investigation.hypotheses import FollowUpTask, Stratification
from packages.investigation.state import (
    CritiqueFinding,
    CritiqueRecord,
    FollowUpApproval,
    FollowUpRoundRecord,
    follow_up_digest,
)
from tests.investigation_churn_schema import churn_v2_plan
from tests.investigation_fixtures import APPROVED_AT, apply_update, running_state

TENURE = Stratification(field="tenure_months_start", cut_points=(6,))
SMB_BASIC = [
    {"field": "customer_segment", "value": "SMB"},
    {"field": "plan_tier", "value": "Basic"},
]


def v2_running_state(**tasks: dict[str, Any]) -> dict[str, Any]:
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
    tasks: list[FollowUpTask], round_index: int, *, hours: float = 1.0
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
