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
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    model_validator,
)

from packages.investigation.budget import BudgetRequest
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
