"""Deterministic follow-up construction and the follow-up approval port (#140 INV-7, C3 and C4).

The Supervisor turns confirmed ``missing_stratification`` findings into follow-up tasks here. No
model authors a task: each follow-up copies its approved target test's resolved metric, filters,
period, and claim, adds the finding's stratification, and gets a fixed ID and success criterion.
The result is validated (``validate_follow_up_round``) and must then be approved, for exactly this
task set, by the trusted ``FollowUpAuthorizer`` before anything is admitted.

``FollowUpAuthorizer`` is a port only. Production has no implementation until INV-10 (#143)
provides the human approval flow, so production admits no follow-up and reports
``follow_up_not_authorized``. Only tests construct ``FollowUpApproval`` values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from packages.investigation.contracts import AgentRole, TaskKind
from packages.investigation.hypotheses import (
    FollowUpTask,
    InvestigationPlanV2,
    SpecIncomplete,
    resolve_test,
)
from packages.investigation.state import (
    MAX_FOLLOW_UPS_IN_ROUND,
    CritiqueFinding,
    FollowUpApproval,
    InvestigationScope,
    follow_up_digest,
)

FOLLOW_UP_CRITERION = "Counts per period, group, and stratum for every band."


class FollowUpBuildError(ValueError):
    """A follow-up finding that cannot become a task. The message is a fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def follow_up_task_id(target: str, round_index: int, position: int) -> str:
    """A fixed, valid task ID for the ``position``-th follow-up of a round."""
    return f"{target[:24]}_r{round_index}s{position}"


def build_follow_ups(
    plan: InvestigationPlanV2,
    findings: Sequence[CritiqueFinding],
    *,
    round_index: int,
) -> tuple[tuple[FollowUpTask, ...], tuple[int, ...]]:
    """Follow-up tasks for a critique's ``follow_up`` findings, and the indexes that justify them.

    Raises ``FollowUpBuildError`` when a finding does not name an approved, resolvable SQL test.
    """
    tasks: list[FollowUpTask] = []
    indexes: list[int] = []
    plan_tasks = {task.id: task for task in plan.tasks}
    for index, finding in enumerate(findings):
        if finding.disposition != "follow_up":
            continue
        target = plan_tasks.get(finding.task_id or "")
        if target is None or target.kind is not TaskKind.HYPOTHESIS_TEST:
            raise FollowUpBuildError("target_unknown")
        if finding.stratification is None:
            raise FollowUpBuildError("stratification_missing")
        try:
            test = resolve_test(plan, target)
        except SpecIncomplete:
            raise FollowUpBuildError("target_unresolved") from None
        spec = {
            "kind": "hypothesis_test",
            "hypothesis_id": test.hypothesis_id,
            "method": "stratified_comparison",
            "metric": test.metric.model_dump(mode="json"),
            "filters": [item.model_dump(mode="json") for item in test.filters],
            "period": test.period.model_dump(mode="json"),
            "claim": test.claim.model_dump(mode="json"),
            "stratification": finding.stratification.model_dump(mode="json"),
        }
        tasks.append(
            FollowUpTask.model_validate(
                {
                    "id": follow_up_task_id(target.id, round_index, len(tasks)),
                    "kind": "hypothesis_test",
                    "assignee": AgentRole.SQL_ANALYST.value,
                    "depends_on": [target.id],
                    "input": spec,
                    "success_criterion": FOLLOW_UP_CRITERION,
                    "max_attempts": 1,
                }
            )
        )
        indexes.append(index)
    if len(tasks) > MAX_FOLLOW_UPS_IN_ROUND:
        raise FollowUpBuildError("too_many_follow_ups")
    return tuple(tasks), tuple(indexes)


@dataclass(frozen=True)
class FollowUpApprovalRequest:
    """What an approver is asked: content-free identity plus the exact typed tasks to approve."""

    scope: InvestigationScope
    plan_digest: str
    round: int
    follow_up_digest: str
    tasks: tuple[FollowUpTask, ...]

    @classmethod
    def of(
        cls,
        scope: InvestigationScope,
        plan_digest: str,
        round_index: int,
        tasks: tuple[FollowUpTask, ...],
    ) -> FollowUpApprovalRequest:
        return cls(scope, plan_digest, round_index, follow_up_digest(tasks), tasks)


class FollowUpAuthorizer(Protocol):
    """The trusted approval port for one follow-up round (decision C3).

    Returns the approval for exactly ``request`` (its digest and round), or ``None`` when the
    round is not approved. Production has no implementation until INV-10 (#143).
    """

    def authorize(self, request: FollowUpApprovalRequest) -> FollowUpApproval | None: ...


__all__ = [
    "FOLLOW_UP_CRITERION",
    "FollowUpApprovalRequest",
    "FollowUpAuthorizer",
    "FollowUpBuildError",
    "build_follow_ups",
    "follow_up_task_id",
]
