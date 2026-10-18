"""INV-4 (#137) S2a: planning authority, deadline, budget, and pricing contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from packages.investigation import (
    EXECUTION_LIMITS,
    PLANNING_LIMITS,
    BudgetClosedError,
    BudgetExhaustedError,
    BudgetLedger,
    BudgetResource,
    InvalidChargeError,
    ProviderCostProfile,
    ProviderPricingTable,
)
from packages.investigation.gateway import (
    ANALYSIS_RUN_CAPABILITY,
    PLANNING_STAGE_INPUT_TOKENS,
    PLANNING_STAGE_OUTPUT_TOKENS,
    AuthorityStatus,
    CancellationToken,
    ConsentSnapshot,
    MeteringUnbounded,
    PlanningAuthority,
    PlanningOutcomeCode,
    PlanningStage,
    PlanningStageReservation,
    ProviderAttemptBound,
    ProviderEnvelope,
    ReviewedPlanningCapability,
    check_planning_authority,
    reserve_planning_stage,
)
from packages.investigation.state import InvestigationScope
from src.llm.base import LLMResponse
from src.llm.bounded import (
    BoundedModelAdapter,
    DeadlineBoundedModelAdapter,
    DeadlineModelResult,
    DeadlineTermination,
    PlanningDeadline,
    admit_planning_adapter,
)
from tests.investigation_fixtures import SCOPE


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class LiveAuthority:
    def __init__(
        self, status: object = AuthorityStatus.VALID, error: Exception | None = None
    ) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[InvestigationScope, int]] = []

    def current(self, scope: InvestigationScope, epoch: int) -> AuthorityStatus:
        self.calls.append((scope, epoch))
        if self.error is not None:
            raise self.error
        return self.status  # type: ignore[return-value]


class Cancellation:
    cancel_requested = False


BOUND = ProviderAttemptBound(
    provider="scripted",
    model="planner-v1",
    data_recipient=None,
    max_attempts=1,
    max_output_tokens=PLANNING_STAGE_OUTPUT_TOKENS,
    input_bounded=True,
)
ENVELOPE = ProviderEnvelope(members=(BOUND,))


def _authority(**overrides: Any) -> PlanningAuthority:
    values: dict[str, Any] = {
        "scope": InvestigationScope.model_validate(SCOPE),
        "capabilities": frozenset({ANALYSIS_RUN_CAPABILITY}),
        "consent": ConsentSnapshot(),
        "required_recipients": (),
        "epoch": 3,
        "provider_envelope": ENVELOPE,
    }
    return PlanningAuthority.model_validate(values | overrides)


def _capability(**overrides: Any) -> ReviewedPlanningCapability:
    values: dict[str, Any] = {
        "bound": BOUND,
        "evidence_version": "scripted-test-v1",
        "provider_deadline_enforced": True,
        "termination_confirmed": True,
    }
    return ReviewedPlanningCapability.model_validate(values | overrides)


class DeadlineAdapter(DeadlineBoundedModelAdapter):
    @property
    def bound(self) -> ProviderAttemptBound:
        return BOUND

    def count_input_tokens(self, prompt: str) -> int:
        return 1

    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        return LLMResponse(text="{}", model=self.model, provider=self.provider)

    def generate_once_with_deadline(
        self,
        prompt: str,
        *,
        max_output_tokens: int,
        deadline: PlanningDeadline,
        cancellation: CancellationToken,
    ) -> DeadlineModelResult:
        return DeadlineModelResult(
            response=self.generate_once(prompt, max_output_tokens=max_output_tokens),
            termination=DeadlineTermination.COMPLETED,
            cancellation_requested=cancellation.cancel_requested,
            termination_confirmed=True,
            provider_execution_bound_enforced=True,
        )


class PlainAdapter(BoundedModelAdapter):
    @property
    def bound(self) -> ProviderAttemptBound:
        return BOUND

    def count_input_tokens(self, prompt: str) -> int:
        return 1

    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        return LLMResponse(text="{}", model=self.model, provider=self.provider)


# --- one cumulative monotonic planning deadline ----------------------------------------------


def test_fresh_ledger_reports_remaining_allowance_that_never_increases() -> None:
    clock = Clock()
    ledger = BudgetLedger(PLANNING_LIMITS, clock=clock)
    assert ledger.remaining_wall_clock() == PLANNING_LIMITS.wall_clock_seconds
    clock.now += 4.25
    assert ledger.remaining_wall_clock() == pytest.approx(25.75)
    assert ledger.usage().elapsed_seconds == pytest.approx(4.25)


def test_remaining_deadline_exhaustion_and_closed_ledgers_fail_closed() -> None:
    clock = Clock()
    ledger = BudgetLedger(PLANNING_LIMITS, clock=clock)
    clock.now += PLANNING_LIMITS.wall_clock_seconds
    with pytest.raises(BudgetExhaustedError) as caught:
        ledger.remaining_wall_clock()
    assert caught.value.resource is BudgetResource.WALL_CLOCK
    assert ledger.closed_reason == "exhausted"
    with pytest.raises(BudgetClosedError) as closed:
        ledger.remaining_wall_clock()
    assert closed.value.reason == "exhausted"


def test_backwards_or_faulty_deadline_clock_closes_the_ledger() -> None:
    clock = Clock()
    ledger = BudgetLedger(PLANNING_LIMITS, clock=clock)
    clock.now -= 0.1
    with pytest.raises(BudgetClosedError) as caught:
        ledger.remaining_wall_clock()
    assert caught.value.reason == "clock_fault"
    with pytest.raises(BudgetClosedError):
        reserve_planning_stage(ledger, PlanningStage.INITIAL)


def test_concurrent_deadline_reads_and_settlements_preserve_ledger_invariants() -> None:
    ledger = BudgetLedger(PLANNING_LIMITS, clock=lambda: 0.0)
    initial = reserve_planning_stage(ledger, PlanningStage.INITIAL)
    repair = reserve_planning_stage(ledger, PlanningStage.REPAIR)

    def read(_: int) -> float:
        return ledger.remaining_wall_clock()

    def settle(item: tuple[int, PlanningStageReservation]) -> None:
        index, staged = item
        ledger.settle(
            staged.reservation,
            input_tokens=100 + index,
            output_tokens=10 + index,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        reads = list(pool.map(read, range(20)))
        list(pool.map(settle, enumerate((initial, repair))))

    usage = ledger.usage()
    assert reads == [PLANNING_LIMITS.wall_clock_seconds] * 20
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (2, 201, 21)
    assert usage.outstanding_reservations == 0


# --- fixed stage reservations ----------------------------------------------------------------


def test_both_stages_share_one_ledger_and_cannot_reset_cumulative_limits() -> None:
    ledger = BudgetLedger(PLANNING_LIMITS, clock=lambda: 0.0)
    initial = reserve_planning_stage(ledger, PlanningStage.INITIAL)
    repair = reserve_planning_stage(ledger, PlanningStage.REPAIR)
    assert initial.reservation.ledger_id == repair.reservation.ledger_id
    assert initial.reservation.request.input_tokens == PLANNING_STAGE_INPUT_TOKENS
    assert repair.reservation.request.output_tokens == PLANNING_STAGE_OUTPUT_TOKENS
    with pytest.raises(BudgetExhaustedError) as caught:
        reserve_planning_stage(ledger, PlanningStage.INITIAL)
    assert caught.value.resource is BudgetResource.MODEL_CALLS
    assert ledger.closed_reason == "exhausted"


def test_stage_reservations_reject_wrong_ledgers_stages_and_caller_amounts() -> None:
    with pytest.raises(InvalidChargeError):
        reserve_planning_stage(BudgetLedger(EXECUTION_LIMITS), PlanningStage.INITIAL)
    with pytest.raises(InvalidChargeError):
        reserve_planning_stage(BudgetLedger(PLANNING_LIMITS), "initial")  # type: ignore[arg-type]
    assert set(PlanningStage) == {PlanningStage.INITIAL, PlanningStage.REPAIR}
    assert set(PlanningOutcomeCode) == {
        PlanningOutcomeCode.PLAN_INVALID,
        PlanningOutcomeCode.PROVIDER_FAILURE,
        PlanningOutcomeCode.SEQUENCE_VIOLATION,
    }


# --- trusted pre-approval authority -----------------------------------------------------------


def test_planning_authority_has_no_approval_budget_or_execution_fields() -> None:
    authority = _authority()
    assert set(type(authority).model_fields) == {
        "scope",
        "capabilities",
        "consent",
        "required_recipients",
        "epoch",
        "provider_envelope",
    }
    for forged in ("approval", "sql_approval_required", "budget", "execution_permissions"):
        with pytest.raises(ValidationError):
            PlanningAuthority.model_validate({**authority.model_dump(), forged: True})


def test_planning_authority_binds_scope_then_capability_consent_and_live_epoch() -> None:
    authority = _authority()
    live = LiveAuthority()
    assert check_planning_authority(authority, authority.scope, live=live) is None
    assert live.calls == [(authority.scope, 3)]

    live = LiveAuthority()
    refusal = check_planning_authority(
        _authority(capabilities=frozenset({"workspace:read"})), authority.scope, live=live
    )
    assert refusal is not None and refusal.value == "capability_missing"
    assert live.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "tenant_b"),
        ("workspace_id", "workspace_b"),
        ("investigation_id", "investigation_b"),
    ],
)
def test_planning_authority_rejects_every_foreign_scope_dimension(field: str, value: str) -> None:
    authority = _authority()
    foreign = authority.scope.model_copy(update={field: value})
    live = LiveAuthority()
    refusal = check_planning_authority(authority, foreign, live=live)
    assert refusal is not None and refusal.value == "scope_mismatch"
    assert live.calls == []


def test_hosted_planning_requires_consent_even_after_validation_bypass() -> None:
    hosted = BOUND.model_copy(update={"data_recipient": "Scripted Hosted"})
    with pytest.raises(ValidationError):
        _authority(provider_envelope=ProviderEnvelope(members=(hosted,)))

    valid = _authority(
        provider_envelope=ProviderEnvelope(members=(hosted,)),
        required_recipients=("Scripted Hosted",),
        consent=ConsentSnapshot(),
    )
    forged = PlanningAuthority.model_construct(**{**dict(valid), "required_recipients": ()})
    live = LiveAuthority()
    refusal = check_planning_authority(forged, forged.scope, live=live)
    assert refusal is not None and refusal.value == "consent_missing"
    assert live.calls == []


def test_malformed_bypassed_authority_fails_closed_before_live_check() -> None:
    valid = _authority()
    forged = PlanningAuthority.model_construct(**{**dict(valid), "capabilities": None})
    live = LiveAuthority()
    refusal = check_planning_authority(forged, valid.scope, live=live)
    assert refusal is not None and refusal.value == "authority_unavailable"
    assert live.calls == []


@pytest.mark.parametrize(
    ("live", "code"),
    [
        (LiveAuthority(AuthorityStatus.REVOKED), "authority_revoked"),
        (LiveAuthority("forged"), "authority_unavailable"),
        (LiveAuthority(error=RuntimeError("private provider message")), "authority_unavailable"),
    ],
)
def test_live_planning_authority_fails_closed_with_content_free_codes(
    live: LiveAuthority, code: str
) -> None:
    refusal = check_planning_authority(_authority(), _authority().scope, live=live)
    assert refusal is not None and refusal.value == code
    assert "private" not in refusal.value


# --- deadline capability and exact provider admission ----------------------------------------


def test_deadline_value_and_result_distinguish_all_enforcement_facts() -> None:
    assert PlanningDeadline(2).remaining_seconds == 2.0
    for invalid in (0, -1, True, float("inf"), float("nan"), "2"):
        with pytest.raises(ValueError):
            PlanningDeadline(invalid)  # type: ignore[arg-type]

    timed_out = DeadlineModelResult(
        response=None,
        termination=DeadlineTermination.PROVIDER_DEADLINE_ENFORCED,
        cancellation_requested=True,
        termination_confirmed=False,
        provider_execution_bound_enforced=False,
    )
    assert timed_out.cancellation_requested
    assert not timed_out.termination_confirmed
    assert not timed_out.provider_execution_bound_enforced
    with pytest.raises(ValueError):
        replace(timed_out, response=LLMResponse(text="secret", model="x", provider="y"))


def test_planning_adapter_requires_separate_reviewed_evidence_and_exact_identity() -> None:
    adapter = DeadlineAdapter()
    assert admit_planning_adapter(adapter, envelope=ENVELOPE, capability=_capability()) is adapter
    with pytest.raises(MeteringUnbounded):
        admit_planning_adapter(adapter, envelope=ENVELOPE, capability=None)
    with pytest.raises(MeteringUnbounded):
        admit_planning_adapter(PlainAdapter(), envelope=ENVELOPE, capability=_capability())
    other = BOUND.model_copy(update={"model": "other"})
    with pytest.raises(MeteringUnbounded):
        admit_planning_adapter(
            adapter,
            envelope=ProviderEnvelope(members=(other,)),
            capability=_capability(),
        )


def test_forged_or_incomplete_deadline_capabilities_fail_closed() -> None:
    for field in ("provider_deadline_enforced", "termination_confirmed"):
        with pytest.raises(ValidationError):
            _capability(**{field: False})
    forged = ReviewedPlanningCapability.model_construct(
        **{**dict(_capability()), "provider_deadline_enforced": False}
    )
    with pytest.raises(MeteringUnbounded):
        admit_planning_adapter(DeadlineAdapter(), envelope=ENVELOPE, capability=forged)
    unbounded = BOUND.model_copy(update={"max_attempts": 2})
    with pytest.raises(ValidationError):
        _capability(bound=unbounded)


# --- trusted versioned pricing ---------------------------------------------------------------


def _profile(**overrides: Any) -> ProviderCostProfile:
    values: dict[str, Any] = {
        "provider": BOUND.provider,
        "model": BOUND.model,
        "input_per_million_usd": None,
        "output_per_million_usd": None,
        "output_cap_enforced": True,
        "input_bounded_before_call": True,
        "retries_metered": True,
    }
    return ProviderCostProfile.model_validate(values | overrides)


def test_versioned_pricing_has_no_defaults_or_identity_inference() -> None:
    table = ProviderPricingTable(version="reviewed-2026.10", profiles=(_profile(),))
    assert table.profile_for(provider=BOUND.provider, model=BOUND.model) == _profile()
    assert table.profile_for(provider=BOUND.provider.upper(), model=BOUND.model) is None
    assert table.profiles[0].input_per_million_usd is None


@pytest.mark.parametrize("version", ["", "contains space", "../secret"])
def test_malformed_pricing_versions_are_rejected(version: str) -> None:
    with pytest.raises(ValidationError):
        ProviderPricingTable(version=version, profiles=(_profile(),))


def test_pricing_profiles_reject_duplicates_unknown_fields_and_invalid_numbers() -> None:
    with pytest.raises(ValidationError):
        ProviderPricingTable(version="v1", profiles=(_profile(), _profile()))
    with pytest.raises(ValidationError):
        ProviderPricingTable.model_validate(
            {"version": "v1", "profiles": [_profile().model_dump()], "authority": "forged"}
        )
    with pytest.raises(ValidationError):
        _profile(input_per_million_usd=True)
