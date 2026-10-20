"""The Supervisor: one initial plan and at most one repair, through ``PlannerPort`` only (INV-4 S3).

The Supervisor is deterministic coordination, not a model gateway. It never sees a prompt, a model
client, a budget, pricing, evidence, authority, or consent: the trusted planning gateway owns all of
them, and the Supervisor holds only the two-operation ``PlannerPort``. Its whole decision is:

1. call ``initial()`` exactly once;
2. a ``proposed`` result, or any terminal refusal, is returned unchanged;
3. an ``invalid`` initial result, and only that, leads to exactly one ``repair()``;
4. the repair result (``proposed``, ``plan_invalid``, or a refusal) is returned unchanged.

There is no retry, loop, or replanning. Every result is checked before it can cause another call:
it must be exactly a ``PlanningCallResult`` whose fields re-validate, and its status must be legal
for the stage that produced it. A result that is not, or a port operation that raises, ends planning
with a fixed ``SupervisorFailureCode`` and no further call. The Supervisor cannot fabricate a
``PlanningCallResult`` for that case, because a result carries the trusted ledger's usage, which
the Supervisor never sees; ``SupervisorOutcome`` therefore carries either the gateway's unchanged
result or the fixed failure code, never both. The lifecycle owner that holds the gateway (S4) closes
it and records the investigation as failed when a failure code is returned.

A ``proposed`` result is an unapproved plan: the Supervisor never approves, schedules, or runs it,
and never changes the plan, its digest, its approval requirements, or its cost estimate.

**Replanning (INV-7, #140).** ``FollowUpPlanner`` is the Supervisor's bounded replanning: it turns a
validated critique's confirmed ``follow_up`` findings into stratified re-tests of approved tests,
deterministically (``build_follow_ups``). It never calls a model, never authors free text, and never
widens a test: the trusted run owner validates the round (``validate_follow_up_round``), obtains
the trusted follow-up approval, and admits it through the guarded ``replan`` step. At most two
rounds exist; the ledger, validation, and state invariants each enforce that.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from packages.investigation import AgentRole, InvestigationProposal, PlanningValidationError
from packages.investigation.gateway import PlanningOutcomeCode, PlanningStage
from packages.investigation.hypotheses import FollowUpTask, InvestigationPlanV2
from packages.investigation.replanning import build_follow_ups
from packages.investigation.state import CritiqueFinding
from src.orchestration.investigation_planning import (
    PlannerGateway,
    PlanningCallResult,
    PlanningCallStatus,
)


class SupervisorFailureCode(StrEnum):
    """Why the Supervisor stopped without a gateway result it could pass on. Content-free."""

    PORT_ERROR = "supervisor_port_error"  # a port operation raised; nothing is retried
    RESULT_INVALID = "supervisor_result_invalid"  # malformed, forged, or out-of-sequence result


@dataclass(frozen=True)
class SupervisorOutcome:
    """Exactly one of the gateway's unchanged ``result`` or a fixed ``failure`` code."""

    result: PlanningCallResult | None
    failure: SupervisorFailureCode | None
    repaired: bool

    def __post_init__(self) -> None:
        if (self.result is None) == (self.failure is None):
            raise ValueError("a supervisor outcome has either a result or a failure")
        if self.failure is not None and not isinstance(self.failure, SupervisorFailureCode):
            raise ValueError("unknown supervisor failure")


# What each stage may legally return. ``invalid`` exists only after the initial call, and
# ``plan_invalid`` only after the repair; a refusal may come from either.
_INITIAL_STATUSES = frozenset(
    {PlanningCallStatus.PROPOSED, PlanningCallStatus.INVALID, PlanningCallStatus.REFUSED}
)
_REPAIR_STATUSES = frozenset(
    {PlanningCallStatus.PROPOSED, PlanningCallStatus.PLAN_INVALID, PlanningCallStatus.REFUSED}
)


def plan_investigation(port: PlannerGateway) -> SupervisorOutcome:
    """Run the fixed initial-then-at-most-one-repair sequence through ``port``.

    Production callers pass ``InvestigationPlanningGateway.planner_port()``, never the gateway
    itself; the S4 graph owner is responsible for that. Either way this function only ever calls
    ``initial()`` and ``repair()``.
    """
    try:
        first = port.initial()
    except Exception:  # the message may carry content: drop it, never retry
        return SupervisorOutcome(
            result=None, failure=SupervisorFailureCode.PORT_ERROR, repaired=False
        )
    if not _legal(first, PlanningStage.INITIAL, _INITIAL_STATUSES):
        return SupervisorOutcome(
            result=None, failure=SupervisorFailureCode.RESULT_INVALID, repaired=False
        )
    if first.status is not PlanningCallStatus.INVALID:
        return SupervisorOutcome(result=first, failure=None, repaired=False)
    try:
        second = port.repair()
    except Exception:
        return SupervisorOutcome(
            result=None, failure=SupervisorFailureCode.PORT_ERROR, repaired=True
        )
    if not _legal(second, PlanningStage.REPAIR, _REPAIR_STATUSES):
        return SupervisorOutcome(
            result=None, failure=SupervisorFailureCode.RESULT_INVALID, repaired=True
        )
    return SupervisorOutcome(result=second, failure=None, repaired=True)


def _legal(result: object, stage: PlanningStage, statuses: frozenset[PlanningCallStatus]) -> bool:
    """Exactly a re-validating ``PlanningCallResult`` with a status legal for ``stage``."""
    if type(result) is not PlanningCallResult:
        return False
    try:
        replace(result)  # re-runs the result's own shape checks on a bypassed instance
    except (AttributeError, TypeError, ValueError):
        return False
    status = result.status
    if not isinstance(status, PlanningCallStatus) or status not in statuses:
        return False
    if status is PlanningCallStatus.REFUSED:
        return True  # a refusal may name either stage (a closed gateway reports its last one)
    if result.stage is not stage:
        return False
    if any(type(error) is not PlanningValidationError for error in result.errors):
        return False
    if status is PlanningCallStatus.PLAN_INVALID:
        return result.code is PlanningOutcomeCode.PLAN_INVALID
    if status is PlanningCallStatus.PROPOSED:
        proposal = result.proposal
        if type(proposal) is not InvestigationProposal:
            return False
        try:
            # ``warnings=False``: a serializer warning would quote the forged value it found
            dumped = proposal.model_dump(warnings=False)
            return InvestigationProposal.model_validate(dumped) == proposal
        except ValueError:
            return False
    return True


class FollowUpPlanner:
    """The Supervisor's deterministic follow-up construction for one round (INV-7 C4)."""

    role = AgentRole.SUPERVISOR

    def plan_round(
        self,
        plan: InvestigationPlanV2,
        findings: tuple[CritiqueFinding, ...],
        *,
        round_index: int,
    ) -> tuple[tuple[FollowUpTask, ...], tuple[int, ...]]:
        """The round's follow-up tasks and the indexes of the findings that justify them.

        Raises ``FollowUpBuildError`` when a finding names no approved, resolvable test.
        """
        return build_follow_ups(plan, findings, round_index=round_index)
