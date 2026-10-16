"""Investigation lifecycle: statuses, legal transitions, and the plan-approval deadline (ADR 0024).

An investigation is proposed (planning, then awaiting approval) and only runs after explicit
approval. Every path ends in exactly one terminal status, and terminal statuses never change: a
retry is a new investigation. ``interrupted`` is where restart reconciliation puts any investigation
that was not terminal, because process-local checkpoints do not survive a restart.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum


class InvestigationStatus(StrEnum):
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_SQL_APPROVAL = "awaiting_sql_approval"
    COMPLETED = "completed"
    COMPLETED_PARTIAL = "completed_partial"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PLAN_INVALID = "plan_invalid"
    PLAN_REJECTED = "plan_rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    EXPIRED = "expired"


S = InvestigationStatus

TERMINAL_STATUSES = frozenset(
    {
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
)

# Cancellation, failure, and restart interruption are reachable from every non-terminal status.
_ALWAYS = frozenset({S.CANCELLED, S.FAILED, S.INTERRUPTED})

TRANSITIONS: dict[InvestigationStatus, frozenset[InvestigationStatus]] = {
    S.PLANNING: _ALWAYS | {S.AWAITING_PLAN_APPROVAL, S.PLAN_INVALID, S.BUDGET_EXHAUSTED},
    S.AWAITING_PLAN_APPROVAL: _ALWAYS | {S.QUEUED, S.PLAN_REJECTED, S.EXPIRED},
    S.QUEUED: _ALWAYS | {S.RUNNING},
    S.RUNNING: _ALWAYS
    | {S.AWAITING_SQL_APPROVAL, S.COMPLETED, S.COMPLETED_PARTIAL, S.BUDGET_EXHAUSTED},
    # Approving or rejecting the SQL both resume the run; rejected tasks end as refused.
    S.AWAITING_SQL_APPROVAL: _ALWAYS | {S.RUNNING, S.EXPIRED},
    **{status: frozenset() for status in TERMINAL_STATUSES},
}

PLAN_APPROVAL_TTL = timedelta(hours=24)


class IllegalTransitionError(ValueError):
    def __init__(self, current: InvestigationStatus, target: InvestigationStatus) -> None:
        super().__init__(f"Illegal investigation transition: {current.value} -> {target.value}.")
        self.current = current
        self.target = target


def is_terminal(status: InvestigationStatus) -> bool:
    return InvestigationStatus(status) in TERMINAL_STATUSES


def transition(current: InvestigationStatus, target: InvestigationStatus) -> InvestigationStatus:
    """Return ``target`` if the move is legal; raise otherwise. Terminal statuses never move.

    Stored values are normalized to members, so an unknown status raises ``ValueError``.
    """
    current, target = InvestigationStatus(current), InvestigationStatus(target)
    if target not in TRANSITIONS[current]:
        raise IllegalTransitionError(current, target)
    return target


def approval_deadline(
    proposed_at: datetime, *, workspace_expires_at: datetime | None = None
) -> datetime:
    """When an unapproved plan expires: 24 hours, or sooner if the workspace expires first."""
    _require_aware(proposed_at, workspace_expires_at)
    deadline = proposed_at + PLAN_APPROVAL_TTL
    if workspace_expires_at is not None and workspace_expires_at < deadline:
        return workspace_expires_at
    return deadline


def approval_expired(deadline: datetime, now: datetime) -> bool:
    """True from the deadline onwards; an approval at exactly the deadline is too late."""
    _require_aware(deadline, now)
    return now >= deadline


def _require_aware(*moments: datetime | None) -> None:
    if any(moment is not None and moment.utcoffset() is None for moment in moments):
        raise ValueError("Investigation timestamps must be timezone-aware.")
