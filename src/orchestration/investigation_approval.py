"""The trusted investigation approval service (ADR 0024 decision 10, INV-10, #143).

``InvestigationApprovalService`` is the only production code that constructs approval authority:
``PlanApprovalGrant``, the state's ``PlanApprovalRecord`` (through the execution owner, copied from
a grant), ``FollowUpApproval``, and ``SQLWaveDecision``. It holds the in-process lifecycle record of
each investigation's decision stage; the proposal checkpoint is immutable evidence and is never
rewritten. Durable records, HTTP endpoints, and restart reconciliation belong to INV-11 (#144).

**Plan approval.** ``register_proposal`` accepts only a trusted ``InvestigationProposalRun`` whose
strict, revalidated thread awaits approval with a ``proposed`` record, and registers it for its
conversation (at most one live investigation per conversation, D4). The proposal expires 24 hours
after registration, or at the workspace's expiry if sooner. ``decide_plan`` then checks, cheapest
and most local first, and refuses with a fixed code at the first failure:

1. the request and approver are exactly the contract types (``request_invalid``,
   ``approver_untrusted``);
2. a proposal for exactly that tenant, workspace, and investigation is registered, and the approver
   belongs to that tenant (``approval_not_pending``, ``tenant_mismatch``);
3. it is still pending: not decided, cancelled, revoked, or expired (``approval_not_pending``,
   ``approval_expired``; an expired proposal moves to ``expired``);
4. the request echoes the exact plan digest and authority epoch (``plan_digest_mismatch``,
   ``epoch_mismatch``), and the proposal thread still validates with that digest
   (``proposal_invalid``);
5. the approver has ``analysis:run`` and the consent snapshot covers every recipient the planning
   authority named (``capability_missing``, ``consent_missing``);
6. the trusted live check, last, is ``valid`` for the registered epoch (``authority_revoked``,
   ``authority_unavailable``).

Approval moves the investigation to ``queued`` and issues a grant whose deadline is the earlier of
24 hours after approval and the workspace's expiry. Rejection moves it to ``plan_rejected``. One
lock serialises every decision with ``cancel`` and ``revoke``, so whichever comes first wins:
cancellation or revocation after an approval revokes the grant, and an approval after either fails.

**Execution.** ``InvestigationExecutionRun`` consumes the grant once (``consume_grant``), and runs
with ``run_cancellation`` and ``run_live`` so a cancellation or revocation here stops the run and
discards a late result. SQL that needs approval is requested per wave (``request_sql_wave``); the
approver sees the wave's statements in process only (``pending_sql_wave``) and answers with
``decide_sql_wave``, which must decide every item of exactly that wave. Follow-up rounds are
authorized only within the rounds the plan approver delegated (``follow_up_authorizer``).
``record_terminal`` records the run's end.

**Audit (D10).** Every operation that happens emits one content-free event: tenant from the
trusted scope, the investigation ID as the resource, and allowlisted counts and fixed tokens. A
refused operation emits nothing: its caller (INV-11's API) records ``authz.denied`` as usual.
``agent.investigation_reconciled`` is never emitted here (INV-11's restart reconciliation).
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from packages.governance.audit import AuditRecorder
from packages.investigation.approval import (
    FOLLOW_UP_APPROVAL_TTL,
    ApprovalErrorCode,
    ApprovalRefused,
    ApproverIdentity,
    PendingPlanApproval,
    PlanApprovalGrant,
    PlanDecisionRequest,
    SQLWaveDecision,
    SQLWaveDecisionRequest,
    SQLWaveItem,
    SQLWaveRequest,
    wave_decision_violation,
)
from packages.investigation.gateway import (
    ANALYSIS_RUN_CAPABILITY,
    AuthorityCheck,
    AuthorityStatus,
    CancellationToken,
    ConsentSnapshot,
    PlanningAuthority,
    WallClock,
)
from packages.investigation.hypotheses import InvestigationPlanV2
from packages.investigation.lifecycle import (
    TERMINAL_STATUSES,
    InvestigationStatus,
    approval_deadline,
    approval_expired,
    transition,
)
from packages.investigation.replanning import FollowUpApprovalRequest, FollowUpAuthorizer
from packages.investigation.state import (
    MAX_FOLLOW_UPS_IN_ROUND,
    FollowUpApproval,
    InvestigationScope,
    InvestigationStateView,
    PlanApprovalRecord,
    follow_up_digest,
    plan_digest,
)
from src.orchestration.investigation_checkpoint import load_investigation_state
from src.orchestration.investigation_proposal import InvestigationProposalRun

S = InvestigationStatus
WaveState = Literal["pending", "decided", "expired", "withdrawn"]
_Key = tuple[str, str, str]


def _random_hex() -> str:
    return secrets.token_hex(16)


def _key(scope: InvestigationScope) -> _Key:
    return (scope.tenant_id, scope.workspace_id, scope.investigation_id)


@dataclass(frozen=True)
class SQLWaveReview:
    """What an approver reviews for one wave, in process only: the request and its statements.

    ``statements`` maps each item's task ID to the exact SQL the gateway validated. It is never
    checkpointed, logged, or audited; INV-11 shows it to the approver and nothing else.
    """

    request: SQLWaveRequest
    statements: Mapping[str, str] = field(repr=False)


@dataclass
class _Wave:
    request: SQLWaveRequest
    statements: dict[str, str]
    decision: SQLWaveDecision | None = None
    closed: bool = False


@dataclass
class _Record:
    pending: PendingPlanApproval
    proposal: InvestigationProposalRun
    consent_recipients: frozenset[str]
    workspace_expires_at: datetime | None
    status: InvestigationStatus = S.AWAITING_PLAN_APPROVAL
    grant: PlanApprovalGrant | None = None
    consumed: bool = False
    cancel_requested: bool = False
    revoked: bool = False
    wave: _Wave | None = None
    waves_requested: int = 0
    rounds_issued: dict[int, str] = field(default_factory=dict)


class InvestigationApprovalService:
    """Trusted, process-local approval authority for investigations; see the module docstring."""

    def __init__(
        self,
        *,
        clock: WallClock,
        live: AuthorityCheck,
        audit: AuditRecorder,
        new_id: Callable[[], str] = _random_hex,
    ) -> None:
        if type(audit) is not AuditRecorder:
            raise TypeError("The approval service records through a trusted AuditRecorder.")
        self._clock = clock
        self._live = live
        self._audit = audit
        self._new_id = new_id
        self._lock = threading.RLock()
        self._records: dict[_Key, _Record] = {}
        self._conversations: dict[tuple[str, str, str], _Key] = {}

    # --- plan stage -------------------------------------------------------------------------------

    def register_proposal(
        self,
        proposal: InvestigationProposalRun,
        *,
        conversation_id: str,
        workspace_expires_at: datetime | None = None,
    ) -> PendingPlanApproval:
        """Register a proposal that awaits approval; emits ``agent.investigation_proposed``."""
        if type(proposal) is not InvestigationProposalRun:
            raise ApprovalRefused("proposal_untrusted")
        authority = proposal.planning_authority
        if type(authority) is not PlanningAuthority:
            raise ApprovalRefused("proposal_untrusted")
        view = self._proposal_view(proposal)
        assert view.scope is not None and view.plan is not None
        if view.scope != authority.scope:
            raise ApprovalRefused("scope_mismatch")
        now = self._now()
        try:
            expires_at = approval_deadline(now, workspace_expires_at=workspace_expires_at)
            pending = PendingPlanApproval(
                scope=view.scope,
                conversation_id=conversation_id,
                plan_digest=plan_digest(view.plan),
                plan_version=2 if isinstance(view.plan, InvestigationPlanV2) else 1,
                task_count=len(view.plan.tasks),
                authority_epoch=authority.epoch,
                registered_at=now,
                expires_at=expires_at,
            )
        except ValueError:
            raise ApprovalRefused("request_invalid") from None  # workspace expired, or bad IDs
        key = _key(view.scope)
        slot = (view.scope.tenant_id, view.scope.workspace_id, conversation_id)
        with self._lock:
            self._expire_due(now)
            if key in self._records:
                raise ApprovalRefused("proposal_not_pending")  # each investigation registers once
            if slot in self._conversations:
                raise ApprovalRefused("conversation_busy")  # D4: one live approval per chat
            self._records[key] = _Record(
                pending=pending,
                proposal=proposal,
                consent_recipients=authority.consent_recipients,
                workspace_expires_at=workspace_expires_at,
            )
            self._conversations[slot] = key
            self._emit(
                "agent.investigation_proposed",
                view.scope,
                plan_version=pending.plan_version,
                task_count=pending.task_count,
            )
        return pending

    def decide_plan(
        self,
        request: PlanDecisionRequest,
        approver: ApproverIdentity,
        *,
        consent: ConsentSnapshot,
    ) -> PlanApprovalGrant | None:
        """Approve (returns the grant) or reject (returns ``None``) a pending plan, or refuse."""
        if type(request) is not PlanDecisionRequest or type(consent) is not ConsentSnapshot:
            raise ApprovalRefused("request_invalid")
        if type(approver) is not ApproverIdentity:
            raise ApprovalRefused("approver_untrusted")
        now = self._now()
        with self._lock:
            self._expire_due(now)
            record = self._records.get(
                (request.tenant_id, request.workspace_id, request.investigation_id)
            )
            if record is None:
                raise ApprovalRefused("approval_not_pending")
            scope = record.pending.scope
            if approver.tenant_id != scope.tenant_id:
                raise ApprovalRefused("tenant_mismatch")
            if record.status is S.EXPIRED:
                raise ApprovalRefused("approval_expired")
            if record.status is not S.AWAITING_PLAN_APPROVAL:
                raise ApprovalRefused("approval_not_pending")
            if request.plan_digest != record.pending.plan_digest:
                raise ApprovalRefused("plan_digest_mismatch")
            if request.authority_epoch != record.pending.authority_epoch:
                raise ApprovalRefused("epoch_mismatch")
            view = self._proposal_view(record.proposal)
            if (
                view.scope != scope
                or view.plan is None
                or plan_digest(view.plan) != record.pending.plan_digest
            ):
                raise ApprovalRefused("proposal_invalid")
            if ANALYSIS_RUN_CAPABILITY not in approver.capabilities:
                raise ApprovalRefused("capability_missing")
            if not consent.satisfies(record.consent_recipients):
                raise ApprovalRefused("consent_missing")
            self._require_live(scope, record.pending.authority_epoch)
            if request.decision == "reject":
                self._finish(record, S.PLAN_REJECTED, reason="rejected")
                return None
            # The earlier of 24 hours after approval and the workspace's expiry.
            deadline = approval_deadline(now, workspace_expires_at=record.workspace_expires_at)
            if approval_expired(deadline, now):  # the workspace expired after registration
                self._finish(record, S.EXPIRED, reason="workspace_expired")
                raise ApprovalRefused("approval_expired")
            grant = PlanApprovalGrant(
                grant_id=self._nonce(),
                scope=scope,
                conversation_id=record.pending.conversation_id,
                plan_digest=record.pending.plan_digest,
                authority_epoch=record.pending.authority_epoch,
                approver_id=approver.subject_id,
                approved_at=now,
                deadline=deadline,
                follow_up_rounds=request.follow_up_rounds,
            )
            record.status = transition(record.status, S.QUEUED)
            record.grant = grant
            self._emit(
                "agent.investigation_approved",
                scope,
                plan_version=record.pending.plan_version,
                task_count=record.pending.task_count,
                follow_up_rounds=grant.follow_up_rounds,
            )
            return grant

    def expire_due(self) -> int:
        """Move every proposal or wave past its deadline to ``expired``; returns how many."""
        now = self._now()
        with self._lock:
            return self._expire_due(now)

    def cancel(self, scope: InvestigationScope) -> None:
        """Cancel a live investigation. Wins over any later approval; stops a running one."""
        self._stop(scope, revoke=False)

    def revoke(self, scope: InvestigationScope) -> None:
        """Revoke a live investigation's authority (a consent or role change, for example)."""
        self._stop(scope, revoke=True)

    def status(self, scope: InvestigationScope) -> InvestigationStatus | None:
        """The decision-stage lifecycle status this service records, or ``None``."""
        with self._lock:
            record = self._records.get(_key(scope))
            return None if record is None else record.status

    def plan_approval_record(self, grant: PlanApprovalGrant) -> PlanApprovalRecord:
        """The state-level approval window of a grant this service issued and still holds."""
        with self._lock:
            self._issued(grant)
            return PlanApprovalRecord(
                plan_digest=grant.plan_digest,
                approved_at=grant.approved_at,
                deadline=grant.deadline,
            )

    # --- execution integration --------------------------------------------------------------------

    def consume_grant(self, grant: PlanApprovalGrant) -> PlanApprovalGrant:
        """Start execution under ``grant``: exactly once, unexpired, and still authorized."""
        now = self._now()
        with self._lock:
            record = self._issued(grant)
            if record.consumed:
                raise ApprovalRefused("grant_consumed")
            if record.status is not S.QUEUED or record.cancel_requested or record.revoked:
                raise ApprovalRefused("grant_revoked")
            if approval_expired(grant.deadline, now):
                self._finish(record, S.FAILED, reason="approval_expired")
                raise ApprovalRefused("approval_expired")
            record.consumed = True
            record.status = transition(record.status, S.RUNNING)
            return grant

    def run_cancellation(
        self, grant: PlanApprovalGrant, token: CancellationToken
    ) -> CancellationToken:
        """``token``, also set once this service cancels or revokes the investigation."""
        with self._lock:
            self._issued(grant)
        return _ServiceCancellation(self, _key(grant.scope), token)

    def run_live(self, grant: PlanApprovalGrant, live: AuthorityCheck) -> AuthorityCheck:
        """``live``, but ``revoked`` once this service cancels or revokes the investigation."""
        with self._lock:
            self._issued(grant)
        return _ServiceLiveCheck(self, _key(grant.scope), live)

    def follow_up_authorizer(self, grant: PlanApprovalGrant) -> FollowUpAuthorizer:
        """The production follow-up authorizer for one approved investigation (D9)."""
        with self._lock:
            self._issued(grant)
        return _FollowUpAuthorizer(self, grant)

    def request_sql_wave(
        self,
        grant: PlanApprovalGrant,
        items: tuple[SQLWaveItem, ...],
        statements: Mapping[str, str],
        *,
        deadline: datetime,
    ) -> SQLWaveRequest:
        """Register one wave awaiting approval; the run moves to ``awaiting_sql_approval``."""
        now = self._now()
        with self._lock:
            record = self._live_record(grant)
            if record.status is not S.RUNNING or (
                record.wave is not None and not record.wave.closed
            ):
                raise ApprovalRefused("wave_not_pending")
            slot = (grant.scope.tenant_id, grant.scope.workspace_id, grant.conversation_id)
            if self._conversations.get(slot) != _key(grant.scope):
                raise ApprovalRefused("conversation_busy")
            if set(statements) != {item.task_id for item in items}:
                raise ApprovalRefused("request_invalid")
            try:
                request = SQLWaveRequest(
                    wave_id=self._nonce(),
                    sequence=record.waves_requested + 1,
                    scope=grant.scope,
                    plan_digest=grant.plan_digest,
                    authority_epoch=grant.authority_epoch,
                    items=items,
                    requested_at=now,
                    deadline=min(deadline, grant.deadline),
                )
            except ValueError:
                raise ApprovalRefused("wave_expired") from None  # nothing left of the window
            record.waves_requested += 1
            record.wave = _Wave(request=request, statements=dict(statements))
            record.status = transition(record.status, S.AWAITING_SQL_APPROVAL)
            return request

    def pending_sql_wave(self, scope: InvestigationScope) -> SQLWaveReview | None:
        """The wave awaiting a decision for ``scope``, with its statements, or ``None``."""
        now = self._now()
        with self._lock:
            self._expire_due(now)
            record = self._records.get(_key(scope))
            wave = None if record is None else record.wave
            if wave is None or wave.closed or wave.decision is not None:
                return None
            if record is None or record.status is not S.AWAITING_SQL_APPROVAL:
                return None
            return SQLWaveReview(request=wave.request, statements=dict(wave.statements))

    def decide_sql_wave(
        self, request: SQLWaveDecisionRequest, approver: ApproverIdentity
    ) -> SQLWaveDecision:
        """Decide every item of exactly the pending wave; emits ``agent.sql_approved/rejected``."""
        if type(request) is not SQLWaveDecisionRequest:
            raise ApprovalRefused("request_invalid")
        if type(approver) is not ApproverIdentity:
            raise ApprovalRefused("approver_untrusted")
        now = self._now()
        with self._lock:
            record = self._records.get(
                (request.tenant_id, request.workspace_id, request.investigation_id)
            )
            if record is None:
                raise ApprovalRefused("wave_not_pending")
            scope = record.pending.scope
            if approver.tenant_id != scope.tenant_id:
                raise ApprovalRefused("tenant_mismatch")
            wave = record.wave
            if (
                wave is None
                or wave.closed
                or wave.decision is not None
                or record.status is not S.AWAITING_SQL_APPROVAL
                or record.cancel_requested
                or record.revoked
            ):
                raise ApprovalRefused("wave_not_pending")
            if (
                request.wave_id != wave.request.wave_id
                or request.wave_digest != wave.request.digest
            ):
                raise ApprovalRefused("wave_mismatch")
            if approval_expired(wave.request.deadline, now):
                raise ApprovalRefused("wave_expired")
            if ANALYSIS_RUN_CAPABILITY not in approver.capabilities:
                raise ApprovalRefused("capability_missing")
            assert record.grant is not None
            self._require_live(scope, record.grant.authority_epoch)
            decision = SQLWaveDecision(
                wave_id=wave.request.wave_id,
                wave_digest=wave.request.digest,
                scope=scope,
                plan_digest=wave.request.plan_digest,
                authority_epoch=wave.request.authority_epoch,
                approver_id=approver.subject_id,
                decided_at=now,
                items=request.items,
            )
            violation = wave_decision_violation(decision, wave.request)
            if violation is not None:
                raise ApprovalRefused(_wave_code(violation))
            wave.decision = decision
            choices = [item.decision for item in decision.items]
            approved = choices.count("approve") + choices.count("edit")
            self._emit(
                "agent.sql_approved" if approved else "agent.sql_rejected",
                scope,
                route="investigation",
                wave_size=len(choices),
                approved_count=choices.count("approve"),
                edited_count=choices.count("edit"),
                rejected_count=choices.count("reject"),
                sql_edited="edit" in choices,
            )
            return decision

    def close_sql_wave(
        self, grant: PlanApprovalGrant, wave_id: str
    ) -> tuple[WaveState, SQLWaveDecision | None]:
        """For the run's resume: the wave's outcome, once. A decided or withdrawn wave resumes the
        run (``running``); an expired one is ended by ``record_terminal``. ``pending`` changes
        nothing, so the run stays paused."""
        now = self._now()
        with self._lock:
            record = self._issued(grant)
            wave = record.wave
            if wave is None or wave.closed or wave.request.wave_id != wave_id:
                raise ApprovalRefused("wave_not_pending")
            state = self._wave_state(record, wave, now)
            if state == "pending":
                return state, None
            wave.closed = True
            if state in {"decided", "withdrawn"} and record.status is S.AWAITING_SQL_APPROVAL:
                record.status = transition(record.status, S.RUNNING)
            return state, wave.decision if state == "decided" else None

    def sql_wave_state(self, grant: PlanApprovalGrant, wave_id: str) -> WaveState:
        """The wave's outcome without consuming it."""
        now = self._now()
        with self._lock:
            record = self._issued(grant)
            wave = record.wave
            if wave is None or wave.request.wave_id != wave_id:
                raise ApprovalRefused("wave_not_pending")
            return self._wave_state(record, wave, now)

    def record_terminal(
        self,
        grant: PlanApprovalGrant,
        status: InvestigationStatus,
        view: InvestigationStateView | None,
    ) -> None:
        """Record the run's terminal status, once; emits the lifecycle and task events."""
        with self._lock:
            record = self._issued(grant)
            if not record.consumed or record.status in TERMINAL_STATUSES:
                return
            if view is not None:
                for task_id, task in view.tasks.items():
                    admitted = view.admitted_task(task_id)
                    if task.attempts < 1 or admitted is None:
                        continue  # never started: nothing was executed
                    self._emit(
                        "agent.task_executed",
                        grant.scope,
                        task_kind=admitted.kind.value,
                        outcome=task.status,
                        attempts=task.attempts,
                        error_category=None
                        if task.outcome is None
                        else task.outcome.error_category,
                    )
            self._finish(record, InvestigationStatus(status), reason=None)

    # --- internals -------------------------------------------------------------------------------

    def _now(self) -> datetime:
        try:
            now = self._clock.now()
        except Exception:
            raise ApprovalRefused("clock_unavailable") from None
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ApprovalRefused("clock_unavailable")
        return now

    def _nonce(self) -> str:
        return self._new_id()

    def _proposal_view(self, proposal: InvestigationProposalRun) -> InvestigationStateView:
        try:
            view = load_investigation_state(proposal.graph, proposal.config)
        except Exception:
            raise ApprovalRefused("proposal_invalid") from None
        record = view.proposal_record
        if (
            view.status is not S.AWAITING_PLAN_APPROVAL
            or record is None
            or record.outcome != "proposed"
            or view.plan is None
            or view.scope is None
            or view.plan_approval is not None
            or record.plan_digest != plan_digest(view.plan)
        ):
            raise ApprovalRefused("proposal_invalid")
        return view

    def _require_live(self, scope: InvestigationScope, epoch: int) -> None:
        try:
            status = self._live.current(scope, epoch)
        except Exception:
            raise ApprovalRefused("authority_unavailable") from None
        if status is AuthorityStatus.REVOKED:
            raise ApprovalRefused("authority_revoked")
        if status is not AuthorityStatus.VALID:
            raise ApprovalRefused("authority_unavailable")

    def _issued(self, grant: PlanApprovalGrant) -> _Record:
        """The record of a grant this service issued: the very instance, not an equal copy."""
        if type(grant) is not PlanApprovalGrant:
            raise ApprovalRefused("grant_untrusted")
        record = self._records.get(_key(grant.scope))
        if record is None or record.grant is not grant:
            raise ApprovalRefused("grant_untrusted")
        return record

    def _live_record(self, grant: PlanApprovalGrant) -> _Record:
        record = self._issued(grant)
        if not record.consumed or record.cancel_requested or record.revoked:
            raise ApprovalRefused("grant_revoked")
        if record.status in TERMINAL_STATUSES:
            raise ApprovalRefused("investigation_closed")
        return record

    def _wave_state(self, record: _Record, wave: _Wave, now: datetime) -> WaveState:
        if record.cancel_requested or record.revoked:
            return "withdrawn"
        if wave.decision is not None:
            return "decided"
        if approval_expired(wave.request.deadline, now):
            return "expired"
        return "pending"

    def _expire_due(self, now: datetime) -> int:
        expired = 0
        for record in list(self._records.values()):
            if record.status is S.AWAITING_PLAN_APPROVAL and approval_expired(
                record.pending.expires_at, now
            ):
                self._finish(record, S.EXPIRED, reason="approval_expired")
                expired += 1
        return expired

    def _stop(self, scope: InvestigationScope, *, revoke: bool) -> None:
        with self._lock:
            record = self._records.get(_key(scope))
            if record is None or record.status in TERMINAL_STATUSES:
                return
            if revoke:
                record.revoked = True
            else:
                record.cancel_requested = True
            if record.consumed:
                return  # the run stops itself and reports its end through ``record_terminal``
            if revoke:
                self._finish(record, S.FAILED, reason="authority_revoked")
            else:
                self._finish(record, S.CANCELLED, reason="cancelled")

    def _finish(self, record: _Record, status: InvestigationStatus, *, reason: str | None) -> None:
        record.status = transition(record.status, status)
        scope = record.pending.scope
        slot = (scope.tenant_id, scope.workspace_id, record.pending.conversation_id)
        if self._conversations.get(slot) == _key(scope):
            del self._conversations[slot]
        if status is S.PLAN_REJECTED:
            self._emit("agent.investigation_rejected", scope, reason=reason)
        elif status is S.CANCELLED:
            self._emit("agent.investigation_cancelled", scope, reason=reason)
        else:
            self._emit(
                "agent.investigation_completed",
                scope,
                investigation_status=status.value,
                reason=reason,
            )

    def _emit(self, action: str, scope: InvestigationScope, **attributes: object) -> None:
        self._audit.record(
            action,
            tenant_id=scope.tenant_id,
            resource_id=scope.investigation_id,
            **({"outcome": "success"} | attributes),
        )

    # used by the bound wrappers below
    def _stopped(self, key: _Key) -> bool:
        with self._lock:
            record = self._records.get(key)
            return record is None or record.cancel_requested or record.revoked

    def _authorize_follow_up(
        self, grant: PlanApprovalGrant, request: FollowUpApprovalRequest
    ) -> FollowUpApproval | None:
        if type(request) is not FollowUpApprovalRequest:
            return None
        try:
            now = self._now()
        except ApprovalRefused:
            return None
        with self._lock:
            try:
                record = self._live_record(grant)
            except ApprovalRefused:
                return None
            tasks = request.tasks
            if (
                request.scope != grant.scope
                or request.plan_digest != grant.plan_digest
                or type(request.round) is not int
                or not 1 <= request.round <= grant.follow_up_rounds
                or request.round in record.rounds_issued
                or not 1 <= len(tasks) <= MAX_FOLLOW_UPS_IN_ROUND
                or follow_up_digest(tasks) != request.follow_up_digest
            ):
                return None
            try:
                self._require_live(grant.scope, grant.authority_epoch)
            except ApprovalRefused:
                return None
            deadline = min(now + FOLLOW_UP_APPROVAL_TTL, grant.deadline)
            if not grant.approved_at <= now < deadline:
                return None
            record.rounds_issued[request.round] = request.follow_up_digest
            return FollowUpApproval(
                follow_up_digest=request.follow_up_digest,
                round=request.round,
                approved_at=now,
                deadline=deadline,
            )


def _wave_code(violation: str) -> ApprovalErrorCode:
    codes: dict[str, ApprovalErrorCode] = {
        "wave_mismatch": "wave_mismatch",
        "wave_incomplete": "wave_incomplete",
        "fingerprint_mismatch": "fingerprint_mismatch",
        "wave_expired": "wave_expired",
    }
    return codes.get(violation, "wave_mismatch")


class _ServiceCancellation:
    """The run's cancellation token: the caller's, or this service's cancel or revocation."""

    def __init__(self, service: InvestigationApprovalService, key: _Key, token: CancellationToken):
        self._service = service
        self._key = key
        self._token = token

    @property
    def cancel_requested(self) -> bool:
        try:
            requested = self._token.cancel_requested is not False
        except Exception:
            return True  # an unreadable token cannot prove the run may continue
        return requested or self._service._stopped(self._key)


class _ServiceLiveCheck:
    """The run's live check: ``revoked`` once this service stopped the investigation."""

    def __init__(self, service: InvestigationApprovalService, key: _Key, live: AuthorityCheck):
        self._service = service
        self._key = key
        self._live = live

    def current(self, scope: InvestigationScope, epoch: int) -> AuthorityStatus:
        if self._service._stopped(self._key):
            return AuthorityStatus.REVOKED
        return self._live.current(scope, epoch)


class _FollowUpAuthorizer:
    """``FollowUpAuthorizer`` for one grant: only rounds the plan approver delegated, each once,
    for its exact task set, inside the plan approval's window."""

    def __init__(self, service: InvestigationApprovalService, grant: PlanApprovalGrant) -> None:
        self._service = service
        self._grant = grant

    def authorize(self, request: FollowUpApprovalRequest) -> FollowUpApproval | None:
        return self._service._authorize_follow_up(self._grant, request)


__all__ = ["InvestigationApprovalService", "SQLWaveReview", "WaveState"]
