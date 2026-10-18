"""INV-3 (#136): gateway authority comes from a trusted adapter and every check fails closed."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from apps.api.authorization import Capability
from packages.investigation import (
    EXECUTION_LIMITS,
    BudgetLedger,
    RefusalCode,
    RefusalSeverity,
)
from packages.investigation.gateway import (
    ANALYSIS_RUN_CAPABILITY,
    AuthorityStatus,
    ConsentSnapshot,
    GatewayRefusal,
    InvestigationAuthority,
    MeteringUnbounded,
    ProviderAttemptBound,
    ProviderEnvelope,
    authorize_state,
    check_authority,
)
from packages.investigation.state import InvestigationScope
from tests.investigation_fixtures import (
    SCOPE,
    FixedClock,
    LiveAuthority,
    churn_plan,
    investigation_authority,
    running_state,
)

BEFORE_DEADLINE = datetime(2026, 10, 17, 12, tzinfo=UTC)
AT_DEADLINE = datetime(2026, 10, 18, 9, tzinfo=UTC)


def _refusal(authority: InvestigationAuthority, state: Any, **kwargs: Any) -> RefusalCode:
    live = kwargs.pop("live", LiveAuthority())
    clock = kwargs.pop("clock", FixedClock(BEFORE_DEADLINE))
    with pytest.raises(GatewayRefusal) as caught:
        authorize_state(authority, state, live=live, clock=clock)
    assert str(caught.value) == caught.value.code.value  # code-only message
    return caught.value.code


# --- vocabulary pins ----------------------------------------------------------------------------


def test_the_capability_string_matches_the_api_policy() -> None:
    assert Capability.ANALYSIS_RUN.value == ANALYSIS_RUN_CAPABILITY


@pytest.mark.parametrize(
    ("accepted", "recipients", "required", "expected"),
    [
        (False, (), (), True),  # local-only: nothing leaves, no consent needed
        (False, ("gemini",), ("gemini",), False),
        (True, ("gemini",), ("gemini",), True),
        (True, ("gemini",), ("gemini", "anthropic"), False),
        (True, (), ("gemini",), False),
    ],
)
def test_consent_mirrors_the_message_route(
    accepted: bool, recipients: tuple[str, ...], required: tuple[str, ...], expected: bool
) -> None:
    snapshot = ConsentSnapshot(
        accepted_at=BEFORE_DEADLINE if accepted else None,
        notice_version="v1" if accepted else None,
        recipients=recipients,
    )
    assert snapshot.satisfies(required) is expected


def test_authority_cannot_be_derived_from_state_channels() -> None:
    state = running_state()
    state["authority"] = {"capabilities": [ANALYSIS_RUN_CAPABILITY]}
    assert _refusal(investigation_authority(), state) is RefusalCode.STATE_INVALID


# --- provider envelope (R1) -------------------------------------------------------------------


def _member(**overrides: Any) -> ProviderAttemptBound:
    fields: dict[str, Any] = {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "data_recipient": "Gemini",
        "max_attempts": 3,
        "max_output_tokens": 1_000,
        "input_bounded": True,
    }
    return ProviderAttemptBound.model_validate(fields | overrides)


def test_a_bounded_envelope_reserves_every_member_and_every_attempt() -> None:
    envelope = ProviderEnvelope(
        members=(_member(), _member(provider="anthropic", max_attempts=2, max_output_tokens=500))
    )
    request = envelope.logical_call_request(max_input_tokens_per_call=4_000)
    assert request.model_calls == 5
    assert request.input_tokens == 5 * 4_000
    assert request.output_tokens == 3 * 1_000 + 2 * 500


@pytest.mark.parametrize(
    "unbounded",
    [
        {"max_attempts": None},
        {"max_output_tokens": None},
        {"input_bounded": False},
    ],
)
def test_any_unbounded_member_refuses_the_whole_call(unbounded: dict[str, Any]) -> None:
    envelope = ProviderEnvelope(members=(_member(), _member(provider="ollama", **unbounded)))
    assert not envelope.bounded
    with pytest.raises(MeteringUnbounded) as caught:
        envelope.logical_call_request(max_input_tokens_per_call=4_000)
    assert caught.value.code is RefusalCode.METERING_UNBOUNDED
    assert caught.value.severity is RefusalSeverity.INVESTIGATION


@pytest.mark.parametrize("cap", [0, -1, True, 1.5])
def test_an_unbounded_or_invalid_input_cap_refuses(cap: Any) -> None:
    with pytest.raises(MeteringUnbounded):
        ProviderEnvelope(members=(_member(),)).logical_call_request(max_input_tokens_per_call=cap)


def test_an_empty_envelope_refuses() -> None:
    with pytest.raises(MeteringUnbounded):
        ProviderEnvelope().logical_call_request(max_input_tokens_per_call=1_000)


def test_the_worst_case_reservation_is_refused_when_the_budget_cannot_cover_it() -> None:
    ledger = BudgetLedger(EXECUTION_LIMITS, clock=lambda: 0.0)
    envelope = ProviderEnvelope(members=(_member(max_attempts=10, max_output_tokens=3_000),))
    request = envelope.logical_call_request(max_input_tokens_per_call=1_000)
    assert request.output_tokens > EXECUTION_LIMITS.output_tokens
    with pytest.raises(Exception, match="output_tokens"):
        ledger.authorize(request)


# --- authorize_state ---------------------------------------------------------------------------


def test_valid_restored_state_is_authorized_and_returned_as_a_typed_view() -> None:
    live = LiveAuthority()
    view = authorize_state(
        investigation_authority(), running_state(), live=live, clock=FixedClock(BEFORE_DEADLINE)
    )
    assert view.plan == churn_plan()
    assert live.calls == [(InvestigationScope.model_validate(SCOPE), 3)]


@pytest.mark.parametrize(
    ("field", "value"),
    [("tenant_id", "tenant_b"), ("workspace_id", "ws_2"), ("dataset_id", "ds_other")],
)
def test_state_bound_to_another_scope_is_refused(field: str, value: str) -> None:
    state = running_state()
    state["scope"] = {**SCOPE, field: value}
    live = LiveAuthority()
    assert _refusal(investigation_authority(), state, live=live) is RefusalCode.SCOPE_MISMATCH
    assert live.calls == []  # never consult the adapter for foreign state


def test_tampered_or_non_json_checkpoints_are_refused_before_any_authority_call() -> None:
    state = running_state()
    state["tasks"]["trend"]["status"] = "succeeded"  # no outcome: inconsistent
    live = LiveAuthority()
    assert _refusal(investigation_authority(), state, live=live) is RefusalCode.STATE_INVALID
    assert _refusal(investigation_authority(), {**running_state(), "status": object()}) is (
        RefusalCode.STATE_INVALID
    )
    assert live.calls == []


def test_an_approval_the_authority_did_not_grant_is_refused() -> None:
    other = investigation_authority(deadline=datetime(2026, 10, 17, 21, tzinfo=UTC))
    assert _refusal(other, running_state()) is RefusalCode.APPROVAL_MISMATCH
    replanned = churn_plan().model_copy(update={"objective": "A different objective."})
    assert _refusal(investigation_authority(plan=replanned), running_state()) is (
        RefusalCode.APPROVAL_MISMATCH
    )


@pytest.mark.parametrize("status", ["awaiting_sql_approval", "queued"])
def test_only_a_running_investigation_may_use_tools(status: str) -> None:
    state = running_state(status=status)
    assert _refusal(investigation_authority(), state) is RefusalCode.STATUS_NOT_RUNNING


def test_terminal_investigations_are_refused() -> None:
    state = running_state(status="cancelled")
    assert _refusal(investigation_authority(), state) is RefusalCode.STATUS_NOT_RUNNING


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"capabilities": frozenset({"workspace:read"})}, RefusalCode.CAPABILITY_MISSING),
        ({"consent": ConsentSnapshot()}, RefusalCode.CONSENT_MISSING),
        (
            {"consent": ConsentSnapshot(accepted_at=BEFORE_DEADLINE, recipients=("Anthropic",))},
            RefusalCode.CONSENT_MISSING,
        ),
    ],
)
def test_missing_capability_or_consent_is_refused_before_the_live_check(
    overrides: dict[str, Any], code: RefusalCode
) -> None:
    live = LiveAuthority()
    assert _refusal(investigation_authority(**overrides), running_state(), live=live) is code
    assert live.calls == []


# --- consent is bound to the trusted provider envelope (F2) -------------------------------------


def _hosted_envelope(*recipients: str | None) -> ProviderEnvelope:
    return ProviderEnvelope(
        members=tuple(
            _member(provider=f"provider{index}", data_recipient=recipient)
            for index, recipient in enumerate(recipients)
        )
    )


@pytest.mark.parametrize(
    ("required", "recipients"),
    [
        ((), ("Gemini",)),  # empty requirement, hosted provider
        (("Gemini",), ("Gemini", "Anthropic")),  # the fallback's recipient is missing
        (("Anthropic",), ("Gemini",)),  # names someone else
    ],
)
def test_an_authority_whose_consent_requirement_misses_a_hosted_provider_is_rejected(
    required: tuple[str, ...], recipients: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError):
        investigation_authority(
            required_recipients=required, provider_envelope=_hosted_envelope(*recipients)
        )


def test_every_envelope_member_must_state_its_data_recipient() -> None:
    fields = _member().model_dump()
    del fields["data_recipient"]
    with pytest.raises(ValidationError):
        ProviderAttemptBound.model_validate(fields)


def test_a_local_only_envelope_needs_no_consent() -> None:
    authority = investigation_authority(
        consent=ConsentSnapshot(),
        required_recipients=(),
        provider_envelope=_hosted_envelope(None),
    )
    assert authority.consent_recipients == frozenset()
    authorize_state(
        authority, running_state(), live=LiveAuthority(), clock=FixedClock(BEFORE_DEADLINE)
    )


def test_consent_is_checked_against_the_envelope_even_if_validation_was_bypassed() -> None:
    # ``model_construct`` skips validation; the envelope's recipients still bind the check.
    valid = investigation_authority()
    bypassed = InvestigationAuthority.model_construct(**{**dict(valid), "required_recipients": ()})
    assert bypassed.consent_recipients == {"Gemini"}
    unconsented = InvestigationAuthority.model_construct(
        **{**dict(valid), "required_recipients": (), "consent": ConsentSnapshot()}
    )
    live = LiveAuthority()
    assert _refusal(unconsented, running_state(), live=live) is RefusalCode.CONSENT_MISSING
    assert live.calls == []


def test_consent_for_the_primary_provider_does_not_cover_a_hosted_fallback() -> None:
    authority = investigation_authority(
        required_recipients=("Gemini", "Anthropic"),
        provider_envelope=_hosted_envelope("Gemini", "Anthropic"),
    )
    assert _refusal(authority, running_state()) is RefusalCode.CONSENT_MISSING


def test_consent_inputs_come_only_from_the_authority() -> None:
    # Nothing in state or a request can name providers or recipients: the state schema has no such
    # channel, and the check reads only the authority's own fields.
    state = running_state()
    state["provider_envelope"] = {"members": []}
    assert _refusal(investigation_authority(), state) is RefusalCode.STATE_INVALID
    assert set(inspect.signature(check_authority).parameters) == {"authority", "live", "clock"}


def test_an_expired_approval_is_refused_at_exactly_the_deadline() -> None:
    clock = FixedClock(AT_DEADLINE)
    assert _refusal(investigation_authority(), running_state(), clock=clock) is (
        RefusalCode.APPROVAL_EXPIRED
    )


def test_a_naive_or_failing_clock_fails_closed() -> None:
    naive = FixedClock(datetime(2026, 10, 17, 12))  # noqa: DTZ001 - deliberately naive
    assert _refusal(investigation_authority(), running_state(), clock=naive) is (
        RefusalCode.AUTHORITY_UNAVAILABLE
    )


@pytest.mark.parametrize(
    ("live", "code"),
    [
        (LiveAuthority(status=AuthorityStatus.REVOKED), RefusalCode.AUTHORITY_REVOKED),
        (LiveAuthority(error=TimeoutError()), RefusalCode.AUTHORITY_UNAVAILABLE),
        (LiveAuthority(error=RuntimeError("db down")), RefusalCode.AUTHORITY_UNAVAILABLE),
        (LiveAuthority(status="valid"), RefusalCode.AUTHORITY_UNAVAILABLE),  # not the enum member
        (LiveAuthority(status=None), RefusalCode.AUTHORITY_UNAVAILABLE),
    ],
)
def test_revoked_or_unavailable_live_authority_fails_closed(
    live: LiveAuthority, code: RefusalCode
) -> None:
    assert _refusal(investigation_authority(), running_state(), live=live) is code


def test_the_live_check_receives_the_authority_scope_and_epoch_only() -> None:
    live = LiveAuthority()
    authority = investigation_authority(epoch=7)
    authorize_state(authority, running_state(), live=live, clock=FixedClock(BEFORE_DEADLINE))
    assert live.calls == [(authority.scope, 7)]


def test_check_authority_ignores_everything_but_the_adapter_inputs() -> None:
    # The signature takes no state, request, or agent output: authority cannot be argued with.
    live = LiveAuthority()
    assert (
        check_authority(investigation_authority(), live=live, clock=FixedClock(BEFORE_DEADLINE))
        is None
    )


def test_refusal_messages_are_codes_only() -> None:
    refusal = GatewayRefusal(RefusalCode.CONSENT_MISSING)
    assert json.loads(json.dumps(str(refusal))) == "consent_missing"
    assert refusal.severity is RefusalSeverity.INVESTIGATION
