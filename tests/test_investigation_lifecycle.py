"""INV-0 (#133): investigation lifecycle transitions and the plan-approval deadline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from packages.investigation import (
    PLAN_APPROVAL_TTL,
    TERMINAL_STATUSES,
    TRANSITIONS,
    IllegalTransitionError,
    InvestigationStatus,
    approval_deadline,
    approval_expired,
    is_terminal,
    transition,
)

S = InvestigationStatus
NON_TERMINAL = [status for status in S if status not in TERMINAL_STATUSES]


def test_every_status_has_a_transition_entry() -> None:
    assert set(TRANSITIONS) == set(S)
    assert set(TERMINAL_STATUSES) == {
        S.COMPLETED,
        S.COMPLETED_PARTIAL,
        S.BUDGET_EXHAUSTED,
        S.PLAN_INVALID,
        S.PLAN_REJECTED,
        S.CANCELLED,
        S.FAILED,
        S.INTERRUPTED,
        S.EXPIRED,
    }


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES), ids=str)
def test_terminal_statuses_never_change(status: InvestigationStatus) -> None:
    assert is_terminal(status)
    assert TRANSITIONS[status] == frozenset()
    for target in S:
        with pytest.raises(IllegalTransitionError):
            transition(status, target)


@pytest.mark.parametrize("status", NON_TERMINAL, ids=str)
def test_cancel_fail_and_restart_interruption_reach_every_live_status(
    status: InvestigationStatus,
) -> None:
    assert not is_terminal(status)
    for target in (S.CANCELLED, S.FAILED, S.INTERRUPTED):
        assert transition(status, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.PLANNING, S.AWAITING_PLAN_APPROVAL),
        (S.PLANNING, S.PLAN_INVALID),
        (S.PLANNING, S.BUDGET_EXHAUSTED),
        (S.AWAITING_PLAN_APPROVAL, S.QUEUED),
        (S.AWAITING_PLAN_APPROVAL, S.PLAN_REJECTED),
        (S.AWAITING_PLAN_APPROVAL, S.EXPIRED),
        (S.QUEUED, S.RUNNING),
        (S.RUNNING, S.AWAITING_SQL_APPROVAL),
        (S.AWAITING_SQL_APPROVAL, S.RUNNING),
        (S.AWAITING_SQL_APPROVAL, S.EXPIRED),
        (S.RUNNING, S.COMPLETED),
        (S.RUNNING, S.COMPLETED_PARTIAL),
        (S.RUNNING, S.BUDGET_EXHAUSTED),
    ],
)
def test_legal_transitions(current: InvestigationStatus, target: InvestigationStatus) -> None:
    assert transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.PLANNING, S.QUEUED),  # execution always needs plan approval
        (S.PLANNING, S.RUNNING),
        (S.AWAITING_PLAN_APPROVAL, S.RUNNING),
        (S.QUEUED, S.COMPLETED),
        (S.RUNNING, S.PLANNING),
        (S.RUNNING, S.QUEUED),
        (S.AWAITING_SQL_APPROVAL, S.COMPLETED),
        (S.RUNNING, S.RUNNING),
    ],
)
def test_illegal_transitions(current: InvestigationStatus, target: InvestigationStatus) -> None:
    with pytest.raises(IllegalTransitionError):
        transition(current, target)


def test_only_approval_leads_from_planning_to_execution() -> None:
    reachable_without_approval = {S.PLANNING}
    frontier = [S.PLANNING]
    while frontier:
        for target in TRANSITIONS[frontier.pop()]:
            if target is S.QUEUED or target in reachable_without_approval:
                continue
            reachable_without_approval.add(target)
            frontier.append(target)
    assert S.RUNNING not in reachable_without_approval
    assert TRANSITIONS[S.AWAITING_PLAN_APPROVAL] & {S.QUEUED} == {S.QUEUED}


PROPOSED = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def test_plan_approval_ttl_is_24_hours() -> None:
    assert PLAN_APPROVAL_TTL == timedelta(hours=24)
    assert approval_deadline(PROPOSED) == PROPOSED + timedelta(hours=24)


def test_workspace_expiry_shortens_the_deadline() -> None:
    sooner = PROPOSED + timedelta(hours=3)
    later = PROPOSED + timedelta(days=3)
    assert approval_deadline(PROPOSED, workspace_expires_at=sooner) == sooner
    assert approval_deadline(PROPOSED, workspace_expires_at=later) == PROPOSED + PLAN_APPROVAL_TTL


def test_approval_expires_at_the_deadline() -> None:
    deadline = approval_deadline(PROPOSED)
    assert not approval_expired(deadline, deadline - timedelta(microseconds=1))
    assert approval_expired(deadline, deadline)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError):
        approval_deadline(datetime(2026, 10, 15, 12, 0))
    with pytest.raises(ValueError):
        approval_expired(PROPOSED, datetime(2026, 10, 16, 12, 0))


def test_stored_values_are_normalized_and_unknown_statuses_rejected() -> None:
    moved = transition("planning", "awaiting_plan_approval")  # type: ignore[arg-type]
    assert moved is S.AWAITING_PLAN_APPROVAL
    assert is_terminal("expired")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        transition("planning", "approved")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        is_terminal("done")  # type: ignore[arg-type]


def test_workspace_expiry_before_proposal_expires_immediately() -> None:
    deadline = approval_deadline(PROPOSED, workspace_expires_at=PROPOSED - timedelta(minutes=1))
    assert approval_expired(deadline, PROPOSED)
