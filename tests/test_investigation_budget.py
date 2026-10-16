"""INV-0 (#133): the budget ledger fails closed, and cost estimates are honest about bounds."""

from __future__ import annotations

import dataclasses
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel, ValidationError

from packages.investigation import (
    EXECUTION_LIMITS,
    PLANNING_LIMITS,
    BudgetClosedError,
    BudgetExhaustedError,
    BudgetLedger,
    BudgetLimits,
    BudgetRequest,
    BudgetResource,
    CostEstimate,
    InvalidChargeError,
    ProviderCostProfile,
    estimate_cost,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _ledger(limits: BudgetLimits = EXECUTION_LIMITS) -> tuple[BudgetLedger, FakeClock]:
    clock = FakeClock()
    return BudgetLedger(limits, clock=clock), clock


CALL = BudgetRequest.model_call(input_tokens=1_000, output_tokens=500)


def test_usage_accumulates_across_retries_and_never_resets() -> None:
    ledger, _ = _ledger()
    for _attempt in range(3):
        ledger.settle(ledger.authorize(CALL), input_tokens=800, output_tokens=200)
    ledger.settle(ledger.authorize(BudgetRequest(sql_executions=1, task_attempts=1)))
    usage = ledger.usage()
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (3, 2_400, 600)
    assert (usage.sql_executions, usage.task_attempts) == (1, 1)
    assert not any(hasattr(ledger, name) for name in ("reset", "restore", "clear"))


def test_limits_are_enforced_before_work_using_outstanding_reservations() -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"model_calls": 2})
    ledger, _ = _ledger(limits)
    ledger.authorize(CALL)
    ledger.authorize(CALL)  # both still in flight: nothing settled yet
    with pytest.raises(BudgetExhaustedError) as refused:
        ledger.authorize(CALL)
    assert refused.value.resource is BudgetResource.MODEL_CALLS
    assert ledger.usage().exhausted_resource is BudgetResource.MODEL_CALLS


def test_token_reservation_beyond_remaining_budget_is_refused() -> None:
    ledger, _ = _ledger(EXECUTION_LIMITS.model_copy(update={"output_tokens": 600}))
    ledger.settle(ledger.authorize(CALL), input_tokens=100, output_tokens=200)
    with pytest.raises(BudgetExhaustedError) as refused:
        ledger.authorize(CALL)  # 200 consumed + 500 reserved > 600
    assert refused.value.resource is BudgetResource.OUTPUT_TOKENS


def test_exhaustion_closes_the_ledger_permanently() -> None:
    ledger, _ = _ledger(EXECUTION_LIMITS.model_copy(update={"sql_executions": 0}))
    with pytest.raises(BudgetExhaustedError):
        ledger.authorize(BudgetRequest(sql_executions=1))
    with pytest.raises(BudgetClosedError) as closed:
        ledger.authorize(CALL)
    assert closed.value.reason == "exhausted"


@pytest.mark.parametrize(
    "build",
    [
        lambda: BudgetRequest(sql_executions=-1),
        lambda: BudgetRequest(retrievals=True),
        lambda: BudgetRequest(task_attempts=1.0),  # type: ignore[arg-type]
        lambda: BudgetRequest(),
        lambda: BudgetRequest(model_calls=1),
        lambda: BudgetRequest(model_calls=1, input_tokens=10),
        lambda: BudgetRequest(input_tokens=10, output_tokens=10),
    ],
    ids=[
        "negative",
        "bool",
        "float",
        "empty",
        "call-without-tokens",
        "call-without-output",
        "tokens-without-call",
    ],
)
def test_invalid_requests_are_rejected(build) -> None:
    with pytest.raises(InvalidChargeError):
        build()


def test_rejected_request_leaves_the_ledger_open_and_unchanged() -> None:
    ledger, _ = _ledger()
    with pytest.raises(InvalidChargeError):
        ledger.authorize({"model_calls": 1})  # type: ignore[arg-type]
    assert ledger.closed_reason is None
    assert ledger.usage().model_calls == 0


@pytest.mark.parametrize(
    "usage",
    [{"input_tokens": -1, "output_tokens": 10}, {"input_tokens": 10, "output_tokens": 1.5}],
    ids=["negative", "float"],
)
def test_invalid_settlement_charges_reservation_and_closes(usage: dict[str, object]) -> None:
    ledger, _ = _ledger()
    with pytest.raises(InvalidChargeError):
        ledger.settle(ledger.authorize(CALL), **usage)  # type: ignore[arg-type]
    totals = ledger.usage()
    assert (totals.input_tokens, totals.output_tokens) == (1_000, 500)
    assert ledger.closed_reason == "invalid_settlement"


def test_tokens_cannot_be_settled_against_a_tool_reservation() -> None:
    ledger, _ = _ledger()
    with pytest.raises(InvalidChargeError):
        ledger.settle(ledger.authorize(BudgetRequest(retrievals=1)), input_tokens=5)
    assert ledger.usage().retrievals == 1
    assert ledger.closed_reason == "invalid_settlement"


def test_a_reservation_settles_exactly_once_on_its_own_ledger() -> None:
    ledger, _ = _ledger()
    other, _ = _ledger()
    reservation = ledger.authorize(CALL)
    with pytest.raises(InvalidChargeError):
        other.settle(reservation, input_tokens=1, output_tokens=1)
    ledger.settle(reservation, input_tokens=1, output_tokens=1)
    with pytest.raises(InvalidChargeError):
        ledger.settle(reservation, input_tokens=1, output_tokens=1)
    assert ledger.usage().model_calls == 1


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "charged"),
    [(None, None, (1_000, 500)), (300, None, (300, 500)), (None, 40, (1_000, 40))],
    ids=["both-missing", "output-missing", "input-missing"],
)
def test_missing_usage_is_charged_at_the_full_reservation(
    input_tokens: int | None, output_tokens: int | None, charged: tuple[int, int]
) -> None:
    ledger, _ = _ledger()
    ledger.settle(ledger.authorize(CALL), input_tokens=input_tokens, output_tokens=output_tokens)
    usage = ledger.usage()
    assert (usage.input_tokens, usage.output_tokens) == charged
    assert usage.unmetered_model_calls == 1
    assert ledger.closed_reason is None


def test_reported_usage_above_the_reservation_is_charged_and_can_exhaust() -> None:
    ledger, _ = _ledger(EXECUTION_LIMITS.model_copy(update={"output_tokens": 600}))
    ledger.settle(ledger.authorize(CALL), input_tokens=10, output_tokens=900)
    usage = ledger.usage()
    assert usage.output_tokens == 900
    assert usage.exhausted_resource is BudgetResource.OUTPUT_TOKENS
    with pytest.raises(BudgetClosedError):
        ledger.authorize(BudgetRequest(retrievals=1))


def test_in_flight_work_still_settles_after_cancellation() -> None:
    ledger, _ = _ledger()
    reservation = ledger.authorize(CALL)
    ledger.close("cancelled")
    with pytest.raises(BudgetClosedError):
        ledger.authorize(CALL)
    usage = ledger.settle(reservation, input_tokens=700, output_tokens=100)
    assert (usage.model_calls, usage.input_tokens, usage.closed_reason) == (1, 700, "cancelled")


def test_wall_clock_uses_elapsed_monotonic_time() -> None:
    ledger, clock = _ledger(EXECUTION_LIMITS.model_copy(update={"wall_clock_seconds": 10.0}))
    clock.now += 9.9
    ledger.settle(ledger.authorize(BudgetRequest(retrievals=1)))
    assert ledger.usage().elapsed_seconds == pytest.approx(9.9)
    clock.now += 0.1
    with pytest.raises(BudgetExhaustedError) as refused:
        ledger.authorize(BudgetRequest(retrievals=1))
    assert refused.value.resource is BudgetResource.WALL_CLOCK


def test_clock_going_backwards_closes_the_ledger() -> None:
    ledger, clock = _ledger()
    clock.now -= 1
    with pytest.raises(BudgetClosedError) as closed:
        ledger.authorize(CALL)
    assert closed.value.reason == "clock_fault"
    clock.now += 5
    with pytest.raises(BudgetClosedError):
        ledger.authorize(CALL)


@pytest.mark.parametrize("reading", [float("nan"), float("inf"), "100"], ids=repr)
def test_unusable_clock_is_rejected(reading: object) -> None:
    with pytest.raises(InvalidChargeError):
        BudgetLedger(EXECUTION_LIMITS, clock=lambda: reading)  # type: ignore[arg-type,return-value]


def test_planning_budget_allows_no_tools_and_is_separate_from_execution() -> None:
    planning, _ = _ledger(PLANNING_LIMITS)
    execution, _ = _ledger(EXECUTION_LIMITS)
    for tool in ("sql_executions", "retrievals", "task_attempts"):
        with pytest.raises((BudgetExhaustedError, BudgetClosedError)):
            planning.authorize(BudgetRequest(**{tool: 1}))
    assert planning.closed_reason == "exhausted"
    execution.settle(execution.authorize(BudgetRequest(sql_executions=1)))
    assert execution.usage().sql_executions == 1


def test_planning_allows_two_model_calls_then_refuses() -> None:
    planning, _ = _ledger(PLANNING_LIMITS)
    for _ in range(2):
        planning.settle(planning.authorize(CALL), input_tokens=900, output_tokens=400)
    with pytest.raises(BudgetExhaustedError):
        planning.authorize(CALL)


def test_concurrent_authorization_never_exceeds_a_limit() -> None:
    ledger, _ = _ledger(EXECUTION_LIMITS.model_copy(update={"retrievals": 5}))

    def attempt(_: int) -> bool:
        try:
            ledger.authorize(BudgetRequest(retrievals=1))
        except (BudgetExhaustedError, BudgetClosedError):
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        granted = sum(pool.map(attempt, range(50)))
    assert granted == 5


@pytest.mark.parametrize(
    "change",
    [{"model_calls": -1}, {"wall_clock_seconds": 0}, {"wall_clock_seconds": float("inf")}],
    ids=["negative", "zero-clock", "infinite-clock"],
)
def test_budget_limits_reject_invalid_values(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BudgetLimits.model_validate({**EXECUTION_LIMITS.model_dump(), **change})


# --- cost estimates --------------------------------------------------------------------------


def _profile(**overrides: object) -> ProviderCostProfile:
    values: dict[str, object] = {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "input_per_million_usd": 0.30,
        "output_per_million_usd": 2.50,
        "output_cap_enforced": True,
        "input_bounded_before_call": True,
        "retries_metered": True,
    }
    return ProviderCostProfile.model_validate({**values, **overrides})


def test_enforced_upper_bound_uses_the_highest_eligible_price() -> None:
    fallback = _profile(provider="anthropic", model="fallback", input_per_million_usd=1.0)
    estimate = estimate_cost(
        EXECUTION_LIMITS, [_profile(), fallback], price_table_version="2026-10"
    )
    assert estimate.kind == "estimate"
    assert estimate.bound == "enforced_upper_bound"
    # 150k input tokens at the fallback's $1.00 + 25k output tokens at $2.50, per million.
    assert estimate.amount_usd == pytest.approx(0.2125)
    assert estimate.limitations == ()
    assert estimate.price_table_version == "2026-10"


@pytest.mark.parametrize(
    ("override", "limitation"),
    [
        ({"output_cap_enforced": False}, "output_cap_not_enforced"),
        ({"input_bounded_before_call": False}, "input_not_bounded"),
        ({"retries_metered": False}, "retries_not_metered"),
    ],
)
def test_unenforceable_fallback_makes_the_estimate_indicative(
    override: dict[str, object], limitation: str
) -> None:
    fallback = _profile(provider="anthropic", model="fallback", **override)
    estimate = estimate_cost(EXECUTION_LIMITS, [_profile(), fallback])
    assert estimate.bound == "indicative"
    assert estimate.amount_usd is not None
    assert estimate.limitations == (limitation,)


def test_unknown_pricing_on_any_fallback_makes_the_estimate_unavailable() -> None:
    fallback = _profile(provider="ollama", model="local", output_per_million_usd=None)
    estimate = estimate_cost(EXECUTION_LIMITS, [_profile(), fallback])
    assert (estimate.bound, estimate.amount_usd) == ("unavailable", None)
    assert estimate.limitations == ("pricing_unknown",)


def test_no_eligible_provider_is_unavailable() -> None:
    estimate = estimate_cost(EXECUTION_LIMITS, [])
    assert (estimate.bound, estimate.limitations) == ("unavailable", ("no_eligible_provider",))


def test_estimate_amount_is_rounded_up_not_down() -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"input_tokens": 1, "output_tokens": 0})
    estimate = estimate_cost(limits, [_profile(input_per_million_usd=0.3)])
    assert estimate.amount_usd == 0.0001  # 0.0000003 rounded up to the next 1/10000 USD


@pytest.mark.parametrize(
    "values",
    [
        {"bound": "enforced_upper_bound", "amount_usd": 1.0, "limitations": ["pricing_unknown"]},
        {"bound": "indicative", "amount_usd": 1.0},
        {"bound": "unavailable", "amount_usd": 1.0, "limitations": ["pricing_unknown"]},
        {"bound": "indicative", "limitations": ["retries_not_metered"]},
        {"bound": "indicative", "amount_usd": 1.0, "kind": "guarantee"},
    ],
    ids=["enforced-with-limits", "indicative-without", "unavailable-amount", "no-amount", "kind"],
)
def test_cost_estimate_cannot_overstate_its_bound(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CostEstimate.model_validate(values)


# --- review regressions ----------------------------------------------------------------------


def test_altered_reservation_cannot_release_unaccounted_budget() -> None:
    ledger, _ = _ledger(EXECUTION_LIMITS.model_copy(update={"sql_executions": 5}))
    issued = ledger.authorize(BudgetRequest(sql_executions=5))
    smaller = dataclasses.replace(issued, request=BudgetRequest(sql_executions=1))
    with pytest.raises(InvalidChargeError):
        ledger.settle(smaller)
    assert ledger.usage().outstanding_reservations == 1
    usage = ledger.settle(issued)
    assert (usage.sql_executions, usage.outstanding_reservations) == (5, 0)
    with pytest.raises(BudgetExhaustedError):
        ledger.authorize(BudgetRequest(sql_executions=1))


@pytest.mark.parametrize("fault", [float("nan"), "later", RuntimeError("clock")], ids=repr)
def test_clock_fault_mid_run_closes_the_ledger(fault: object) -> None:
    readings: list[object] = [0.0, fault, 1.0]

    def clock() -> float:
        reading = readings.pop(0)
        if isinstance(reading, Exception):
            raise reading
        return reading  # type: ignore[return-value]

    ledger = BudgetLedger(EXECUTION_LIMITS, clock=clock)
    with pytest.raises(BudgetClosedError) as closed:
        ledger.authorize(BudgetRequest(retrievals=1))
    assert closed.value.reason == "clock_fault"
    with pytest.raises(BudgetClosedError):
        ledger.authorize(BudgetRequest(retrievals=1))  # a sane reading does not reopen it


def test_close_rejects_unknown_reasons() -> None:
    ledger, _ = _ledger()
    with pytest.raises(ValueError):
        ledger.close("bogus")  # type: ignore[arg-type]
    assert ledger.closed_reason is None


def test_unmetered_calls_are_never_charged_below_their_reservation() -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"input_tokens": 2_500, "output_tokens": 1_000})
    ledger, _ = _ledger(limits)
    ledger.settle(ledger.authorize(CALL))  # no usage reported: charged 1_000 / 500
    ledger.settle(ledger.authorize(CALL), input_tokens=None, output_tokens=None)
    usage = ledger.usage()
    assert (usage.input_tokens, usage.output_tokens, usage.unmetered_model_calls) == (
        2_000,
        1_000,
        2,
    )
    with pytest.raises(BudgetExhaustedError):
        ledger.authorize(CALL)  # the worst-case charges exhausted the output budget
    with pytest.raises(BudgetClosedError):
        ledger.authorize(BudgetRequest(retrievals=1))


def test_retries_during_replanning_exhaust_task_attempts() -> None:
    ledger, _ = _ledger(EXECUTION_LIMITS.model_copy(update={"task_attempts": 4}))
    for _first_attempt in range(3):
        ledger.settle(ledger.authorize(BudgetRequest(task_attempts=1)))
    ledger.settle(ledger.authorize(BudgetRequest(replan_rounds=1)))
    ledger.settle(ledger.authorize(BudgetRequest(task_attempts=1)))  # retry of a follow-up
    with pytest.raises(BudgetExhaustedError) as refused:
        ledger.authorize(BudgetRequest(task_attempts=1))  # the next admitted follow-up
    assert refused.value.resource is BudgetResource.TASK_ATTEMPTS
    assert ledger.usage().task_attempts == 4


def test_third_replan_round_is_refused() -> None:
    ledger, _ = _ledger()
    for _round in range(EXECUTION_LIMITS.replan_rounds):
        ledger.settle(ledger.authorize(BudgetRequest(replan_rounds=1)))
    with pytest.raises(BudgetExhaustedError) as refused:
        ledger.authorize(BudgetRequest(replan_rounds=1))
    assert refused.value.resource is BudgetResource.REPLAN_ROUNDS


@pytest.mark.parametrize(
    ("model", "base", "change"),
    [
        (BudgetLimits, EXECUTION_LIMITS.model_dump(), {"model_calls": "5"}),
        (BudgetLimits, EXECUTION_LIMITS.model_dump(), {"sql_executions": True}),
        (BudgetLimits, EXECUTION_LIMITS.model_dump(), {"retrievals": 2.0}),
        (BudgetLimits, EXECUTION_LIMITS.model_dump(), {"wall_clock_seconds": "30"}),
        (ProviderCostProfile, {"provider": "p", "model": "m"}, {"input_per_million_usd": "0.3"}),
        (ProviderCostProfile, {"provider": "p", "model": "m"}, {"output_cap_enforced": "yes"}),
        (ProviderCostProfile, {"provider": "p", "model": "m"}, {"retries_metered": 1}),
        (ProviderCostProfile, {"provider": "p", "model": "m"}, {"input_per_million_usd": True}),
        (
            CostEstimate,
            {"bound": "indicative", "limitations": ["pricing_unknown"]},
            {"amount_usd": "1"},
        ),
    ],
    ids=[
        "limit-numeric-string",
        "limit-bool",
        "limit-float",
        "clock-string",
        "price-string",
        "flag-string",
        "flag-int",
        "price-bool",
        "amount-string",
    ],
)
def test_budget_models_reject_coercion(
    model: type[BaseModel], base: dict[str, object], change: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**base, **change})
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps({**base, **change}))


def test_whole_number_json_values_remain_valid_for_float_fields() -> None:
    limits = BudgetLimits.model_validate_json(
        json.dumps({**EXECUTION_LIMITS.model_dump(), "wall_clock_seconds": 240})
    )
    assert limits.wall_clock_seconds == 240.0
    profile = ProviderCostProfile.model_validate(
        {"provider": "p", "model": "m", "input_per_million_usd": 1}
    )
    assert profile.input_per_million_usd == 1.0


def test_estimate_is_exact_where_binary_floats_are_not() -> None:
    estimate = estimate_cost(
        EXECUTION_LIMITS, [_profile(input_per_million_usd=1.0, output_per_million_usd=2.5)]
    )
    assert estimate.amount_usd == 0.2125  # 150_000 * 1.0 + 25_000 * 2.5, per million, exactly
    tiny = estimate_cost(
        EXECUTION_LIMITS.model_copy(update={"input_tokens": 1_000_000, "output_tokens": 0}),
        [_profile(input_per_million_usd=0.1)],
    )
    assert tiny.amount_usd == 0.1
