"""Tool-gateway authority, provider-attempt envelopes, and pure pre-flight checks (ADR 0024, #136).

The investigation tool gateway (``src/orchestration/tool_gateway.py``, S5) decides whether one
operation may run. This module holds the framework-neutral parts of that decision:

- ``InvestigationAuthority``: what the caller is allowed to do, built by a **trusted adapter** (the
  API, from the verified identity, the workspace record, and the plan approval). It is never read
  from investigation state or from agent output; state is only *compared* against it.
- ``ProviderEnvelope``: every provider and attempt one logical model call may use. A model call is
  admitted only when the whole envelope is bounded (a known attempt count, an enforced output cap,
  and bounded input for every member); its reservation is the envelope's worst case. Each member
  names its consent ``data_recipient``, and the authority's consent requirement must cover them.
- ``authorize_state``: in this order, revalidates restored state (``state_invalid``); binds it to
  the authority (scope, approval present, approval and plan digest match, status ``running``); then
  checks the authority itself (capability, consent, approval expiry, and finally the live check).
  The first failure raises a code-only ``GatewayRefusal``, so invalid or foreign state never
  reaches the live check.

The checks here run before any tool, budget reservation, or resource resolution.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    model_validator,
)

from packages.investigation.budget import (
    PLANNING_LIMITS,
    BudgetLedger,
    BudgetRequest,
    InvalidChargeError,
    Reservation,
)
from packages.investigation.lifecycle import InvestigationStatus, approval_expired
from packages.investigation.refusals import RefusalCode, RefusalSeverity, severity_of
from packages.investigation.state import (
    InvestigationScope,
    InvestigationStateView,
    PlanApprovalRecord,
    StateInvalid,
    plan_digest,
    restore_state,
)

# Mirrors ``apps.api.authorization.Capability.ANALYSIS_RUN`` (pinned by a test; ``packages`` cannot
# import ``apps``).
ANALYSIS_RUN_CAPABILITY = "analysis:run"

MAX_PROVIDER_MEMBERS = 8
MAX_ATTEMPTS_PER_MEMBER = 10
PLANNING_STAGE_INPUT_TOKENS = 6_000
PLANNING_STAGE_OUTPUT_TOKENS = 1_000

Token = Annotated[str, StringConstraints(min_length=1, max_length=64)]
StrictPositive = Annotated[int, Strict(), Field(ge=1)]


class _Authority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GatewayRefusal(Exception):
    """A refusal before anything ran. The message is the code alone."""

    def __init__(self, code: RefusalCode) -> None:
        self.code = RefusalCode(code)
        super().__init__(self.code.value)

    @property
    def severity(self) -> RefusalSeverity:
        return severity_of(self.code)


class ConsentSnapshot(_Authority):
    """The workspace's data-sharing consent as the trusted adapter read it."""

    accepted_at: AwareDatetime | None = None
    notice_version: Token | None = None
    recipients: tuple[Token, ...] = Field(default=(), max_length=16)

    def satisfies(self, required_recipients: Collection[str]) -> bool:
        """Mirrors the message route: consent is needed only when data leaves for a recipient."""
        if not required_recipients:
            return True
        return self.accepted_at is not None and set(required_recipients) <= set(self.recipients)


class ProviderAttemptBound(_Authority):
    """What is known about one provider that may serve a logical call (fallbacks included).

    ``max_attempts`` counts the first call plus every retry the client may make; ``None`` means the
    count is unknown. ``max_output_tokens`` is the per-call cap the provider is configured to
    enforce; ``None`` means there is none. ``input_bounded`` means the prompt reaching this provider
    is bounded before the call.

    ``data_recipient`` is the consent recipient that receives uploaded-data content when this
    member is called (the name ``config.settings.Settings.hosted_recipients`` gives it), or
    ``None`` for a provider that keeps data on this machine. The trusted adapter must state it for
    every member: there is no default, so an omitted recipient cannot silently mean "local".
    """

    provider: Token
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    data_recipient: Token | None
    max_attempts: Annotated[int, Strict(), Field(ge=1, le=MAX_ATTEMPTS_PER_MEMBER)] | None = None
    max_output_tokens: StrictPositive | None = None
    input_bounded: Annotated[bool, Strict()] = False

    @property
    def bounded(self) -> bool:
        return (
            self.max_attempts is not None
            and self.max_output_tokens is not None
            and self.input_bounded
        )


class MeteringUnbounded(GatewayRefusal):
    def __init__(self) -> None:
        super().__init__(RefusalCode.METERING_UNBOUNDED)


class ProviderEnvelope(_Authority):
    """Every provider attempt one logical model call may make, in fallback order."""

    members: tuple[ProviderAttemptBound, ...] = Field(default=(), max_length=MAX_PROVIDER_MEMBERS)

    @property
    def bounded(self) -> bool:
        return bool(self.members) and all(member.bounded for member in self.members)

    @property
    def data_recipients(self) -> frozenset[str]:
        """Every recipient that may receive data during one logical call (fallbacks included)."""
        return frozenset(
            member.data_recipient for member in self.members if member.data_recipient is not None
        )

    def logical_call_request(self, *, max_input_tokens_per_call: int) -> BudgetRequest:
        """The worst-case reservation for one logical call, or ``MeteringUnbounded``.

        Every member may be tried, and each may use every attempt, each with up to the input cap
        and its own output cap. Nothing is called guaranteed unless every member is bounded.
        """
        if (
            not self.bounded
            or type(max_input_tokens_per_call) is not int
            or max_input_tokens_per_call < 1
        ):
            raise MeteringUnbounded()
        calls = 0
        output_tokens = 0
        for member in self.members:
            attempts, output_cap = member.max_attempts, member.max_output_tokens
            if attempts is None or output_cap is None:  # unreachable after ``bounded``; fail closed
                raise MeteringUnbounded()
            calls += attempts
            output_tokens += attempts * output_cap
        return BudgetRequest(
            model_calls=calls,
            input_tokens=calls * max_input_tokens_per_call,
            output_tokens=output_tokens,
        )


class PlanningStage(StrEnum):
    """The only two planning reservations; sequencing is enforced by the later gateway."""

    INITIAL = "initial"
    REPAIR = "repair"


class PlanningOutcomeCode(StrEnum):
    """Planning-only terminal outcomes that do not widen execution refusal semantics."""

    PLAN_INVALID = "plan_invalid"
    PROVIDER_FAILURE = "provider_failure"
    SEQUENCE_VIOLATION = "planning_sequence_violation"


@dataclass(frozen=True)
class PlanningStageReservation:
    """A stage label bound to a reservation issued by the shared planning ledger."""

    stage: PlanningStage
    reservation: Reservation


def reserve_planning_stage(ledger: BudgetLedger, stage: PlanningStage) -> PlanningStageReservation:
    """Reserve one fixed 6k/1k stage against the caller's cumulative planning ledger.

    The function accepts no caller-supplied amounts. It deliberately does not implement the
    initial/repair state machine; the S2 gateway will own that sequencing while retaining this
    single ledger for both stages.
    """
    if not isinstance(ledger, BudgetLedger) or ledger.limits != PLANNING_LIMITS:
        raise InvalidChargeError("Planning stages require the fixed planning ledger.")
    if not isinstance(stage, PlanningStage):
        raise InvalidChargeError("Unknown planning stage.")
    reservation = ledger.authorize(
        BudgetRequest.model_call(
            input_tokens=PLANNING_STAGE_INPUT_TOKENS,
            output_tokens=PLANNING_STAGE_OUTPUT_TOKENS,
        )
    )
    return PlanningStageReservation(stage=stage, reservation=reservation)


class AuthorityStatus(StrEnum):
    VALID = "valid"
    REVOKED = "revoked"


class InvestigationAuthority(_Authority):
    """What a trusted adapter granted this investigation. Never derived from state or agents.

    ``epoch`` changes whenever the adapter re-issues authority (for example after a consent or role
    change), so handles issued under an older epoch can be recognised as stale.

    ``required_recipients`` must name every ``data_recipient`` in ``provider_envelope``: an
    authority whose consent requirement leaves out a provider that may receive data is rejected at
    construction. Consent is then checked against the union of both (``consent_recipients``), so
    the envelope binds the requirement even if the field was bypassed.
    """

    scope: InvestigationScope
    capabilities: frozenset[Token]
    consent: ConsentSnapshot
    required_recipients: tuple[Token, ...] = Field(default=(), max_length=16)
    approval: PlanApprovalRecord
    epoch: Annotated[int, Strict(), Field(ge=0)]
    sql_approval_required: Annotated[bool, Strict()]
    provider_envelope: ProviderEnvelope

    @model_validator(mode="after")
    def _consent_covers_the_envelope(self) -> InvestigationAuthority:
        if not self.provider_envelope.data_recipients <= set(self.required_recipients):
            raise ValueError("required_recipients must name every provider data recipient")
        return self

    @property
    def consent_recipients(self) -> frozenset[str]:
        """Who must have consent before data leaves: adapter-declared and envelope-derived."""
        return frozenset(self.required_recipients) | self.provider_envelope.data_recipients


class PlanningAuthority(_Authority):
    """Trusted pre-approval authority for proposal generation only.

    It intentionally has no plan approval, SQL approval, budget, price, prompt, or execution
    permission. The trusted intake adapter binds it to the separately constructed planning
    context; models and agents never construct it.
    """

    scope: InvestigationScope
    capabilities: frozenset[Token]
    consent: ConsentSnapshot
    required_recipients: tuple[Token, ...] = Field(default=(), max_length=16)
    epoch: Annotated[int, Strict(), Field(ge=0)]
    provider_envelope: ProviderEnvelope

    @model_validator(mode="after")
    def _consent_covers_the_envelope(self) -> PlanningAuthority:
        if not self.provider_envelope.data_recipients <= set(self.required_recipients):
            raise ValueError("required_recipients must name every provider data recipient")
        return self

    @property
    def consent_recipients(self) -> frozenset[str]:
        return frozenset(self.required_recipients) | self.provider_envelope.data_recipients


class ReviewedPlanningCapability(_Authority):
    """Separately reviewed provider evidence; never an adapter self-declaration.

    Literal true fields make absence explicit. They record the result of provider-specific review,
    but do not prove it at runtime; the later gateway must still verify every observable report.
    """

    bound: ProviderAttemptBound
    evidence_version: Token
    provider_deadline_enforced: Literal[True]
    termination_confirmed: Literal[True]

    @model_validator(mode="after")
    def _planning_bound(self) -> ReviewedPlanningCapability:
        if (
            self.bound.max_attempts != 1
            or self.bound.input_bounded is not True
            or self.bound.max_output_tokens != PLANNING_STAGE_OUTPUT_TOKENS
        ):
            raise ValueError("reviewed planning capability must match the fixed stage bound")
        return self


class ReviewedPlanningEvidence(_Authority):
    """The trusted, versioned set of reviewed planning capabilities; the one bounded-path record.

    The configuration adapter that holds the current review constructs it; models, agents,
    pricing tables and adapters never do. Every capability must carry the set's
    ``evidence_version``, so a stale or mixed review is rejected rather than partly trusted, and
    each provider/model is reviewed at most once. ``covering`` binds by exact equality with the
    envelope member, the same identity ``admit_planning_adapter`` requires, so a cost estimate
    and a later admission cannot disagree about which path is bounded.
    """

    evidence_version: Token
    capabilities: tuple[ReviewedPlanningCapability, ...] = Field(
        min_length=1, max_length=MAX_PROVIDER_MEMBERS
    )

    @model_validator(mode="after")
    def _current_unique_reviews(self) -> ReviewedPlanningEvidence:
        if any(item.evidence_version != self.evidence_version for item in self.capabilities):
            raise ValueError("every reviewed capability must carry the current evidence version")
        identities = [(item.bound.provider, item.bound.model) for item in self.capabilities]
        if len(identities) != len(set(identities)):
            raise ValueError("provider/model capability identities must be unique")
        return self

    def covering(self, member: ProviderAttemptBound) -> ReviewedPlanningCapability | None:
        """The review whose bound is exactly ``member``; aliases and near matches never count."""
        return next((item for item in self.capabilities if item.bound == member), None)


class AuthorityCheck(Protocol):
    """Live re-check by the trusted adapter. Raising, or returning a non-status, refuses."""

    def current(self, scope: InvestigationScope, epoch: int) -> AuthorityStatus: ...


class CancellationToken(Protocol):
    """Cooperative cancellation; ``packages.jobs.JobContext`` satisfies it."""

    @property
    def cancel_requested(self) -> bool: ...


class WallClock(Protocol):
    def now(self) -> datetime:
        """A timezone-aware current time, for approval expiry."""


def check_binding(
    authority: InvestigationAuthority, view: InvestigationStateView
) -> RefusalCode | None:
    """Compare restored state with the authority. State is checked here, never trusted."""
    if view.scope != authority.scope:
        return RefusalCode.SCOPE_MISMATCH
    if view.plan_approval is None:
        return RefusalCode.APPROVAL_MISSING
    if (
        view.plan_approval != authority.approval
        or view.plan is None
        or plan_digest(view.plan) != authority.approval.plan_digest
    ):
        return RefusalCode.APPROVAL_MISMATCH
    if view.status is not InvestigationStatus.RUNNING:
        return RefusalCode.STATUS_NOT_RUNNING
    return None


def check_authority(
    authority: InvestigationAuthority, *, live: AuthorityCheck, clock: WallClock
) -> RefusalCode | None:
    """Capability, consent, approval expiry, then the live check; adapter inputs only."""
    if ANALYSIS_RUN_CAPABILITY not in authority.capabilities:
        return RefusalCode.CAPABILITY_MISSING
    if not authority.consent.satisfies(authority.consent_recipients):
        return RefusalCode.CONSENT_MISSING
    try:
        if approval_expired(authority.approval.deadline, clock.now()):
            return RefusalCode.APPROVAL_EXPIRED
    except Exception:  # a clock fault cannot prove the approval is still valid
        return RefusalCode.AUTHORITY_UNAVAILABLE
    try:
        status = live.current(authority.scope, authority.epoch)
    except Exception:  # unavailable is not "still valid": fail closed
        return RefusalCode.AUTHORITY_UNAVAILABLE
    if status is AuthorityStatus.REVOKED:
        return RefusalCode.AUTHORITY_REVOKED
    if status is not AuthorityStatus.VALID:
        return RefusalCode.AUTHORITY_UNAVAILABLE
    return None


def check_planning_authority(
    authority: PlanningAuthority,
    scope: InvestigationScope,
    *,
    live: AuthorityCheck,
) -> RefusalCode | None:
    """Bind scope, then check capability, consent, and live authority for planning.

    There is no approval-expiry check because proposal generation precedes plan approval. Every
    input comes from trusted server-side construction; malformed bypassed values fail closed.
    """
    if not isinstance(authority, PlanningAuthority) or not isinstance(scope, InvestigationScope):
        return RefusalCode.AUTHORITY_UNAVAILABLE
    if scope != authority.scope:
        return RefusalCode.SCOPE_MISMATCH
    try:
        if ANALYSIS_RUN_CAPABILITY not in authority.capabilities:
            return RefusalCode.CAPABILITY_MISSING
        recipients = authority.consent_recipients
        if not authority.consent.satisfies(recipients):
            return RefusalCode.CONSENT_MISSING
    except Exception:
        return RefusalCode.AUTHORITY_UNAVAILABLE
    try:
        status = live.current(authority.scope, authority.epoch)
    except Exception:
        return RefusalCode.AUTHORITY_UNAVAILABLE
    if status is AuthorityStatus.REVOKED:
        return RefusalCode.AUTHORITY_REVOKED
    if status is not AuthorityStatus.VALID:
        return RefusalCode.AUTHORITY_UNAVAILABLE
    return None


def authorize_state(
    authority: InvestigationAuthority,
    raw_state: Mapping[str, Any],
    *,
    live: AuthorityCheck,
    clock: WallClock,
) -> InvestigationStateView:
    """Revalidate restored state, bind it to the authority, and re-check authority; or refuse.

    Cheap local checks run before the live check, so invalid or foreign state never reaches the
    trusted adapter.
    """
    try:
        view = restore_state(raw_state)
    except StateInvalid:
        raise GatewayRefusal(RefusalCode.STATE_INVALID) from None
    code = check_binding(authority, view) or check_authority(authority, live=live, clock=clock)
    if code is not None:
        raise GatewayRefusal(code)
    return view
