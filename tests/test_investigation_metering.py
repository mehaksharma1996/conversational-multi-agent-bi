"""INV-3 (#136): ledger call accounting and the provider-call meter are additive and fail closed."""

from __future__ import annotations

from typing import Any, get_args

import pytest

from packages.investigation import (
    EXECUTION_LIMITS,
    LIFECYCLE_TARGET,
    BudgetClosedError,
    BudgetLedger,
    BudgetRequest,
    InvalidChargeError,
)
from packages.investigation.budget import CloseReason, ReservationExceededError
from packages.investigation.refusals import LEDGER_CLOSE_REASON, RefusalCode
from src.llm.base import LLMGenerationError, LLMResponse
from src.llm.fallback import FallbackLLMClient
from src.llm.observability import (
    meter_provider_calls,
    report_fallbacks,
    report_served_by,
    report_usage,
    tap_usage,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingMeter:
    def __init__(self) -> None:
        self.usage: list[tuple[int | None, int | None, int]] = []
        self.fallbacks: list[int] = []

    def record_usage(
        self, prompt_tokens: int | None, output_tokens: int | None, retries: int
    ) -> None:
        self.usage.append((prompt_tokens, output_tokens, retries))

    def record_fallbacks(self, count: int) -> None:
        self.fallbacks.append(count)


def _ledger() -> BudgetLedger:
    return BudgetLedger(EXECUTION_LIMITS, clock=FakeClock())


def _worst_case() -> BudgetRequest:
    # Two providers, the first allowed one retry: at most three provider calls for one logical call.
    return BudgetRequest(model_calls=3, input_tokens=3_000, output_tokens=1_500)


# --- ledger: actual provider-call accounting -------------------------------------------------


def test_settlement_charges_the_metered_call_count_within_the_reservation() -> None:
    ledger = _ledger()
    usage = ledger.settle(
        ledger.authorize(_worst_case()), model_calls=2, input_tokens=900, output_tokens=200
    )
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (2, 900, 200)
    assert usage.unmetered_model_calls == 0
    assert ledger.closed_reason is None


def test_settlement_without_a_call_count_charges_every_reserved_call() -> None:
    ledger = _ledger()
    usage = ledger.settle(ledger.authorize(_worst_case()), input_tokens=900, output_tokens=200)
    assert usage.model_calls == 3


def test_unknown_usage_is_still_charged_in_full_when_calls_are_counted() -> None:
    ledger = _ledger()
    usage = ledger.settle(ledger.authorize(_worst_case()), model_calls=1)
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (1, 3_000, 1_500)
    assert usage.unmetered_model_calls == 1


def test_more_metered_calls_than_reserved_are_charged_as_metered_and_close() -> None:
    ledger = _ledger()
    with pytest.raises(ReservationExceededError):
        ledger.settle(ledger.authorize(_worst_case()), model_calls=4)
    usage = ledger.usage()
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (4, 3_000, 1_500)
    assert usage.closed_reason == "reservation_exceeded"
    assert usage.reservation_bound == "exceeded"


@pytest.mark.parametrize("model_calls", [0, -1, True, 1.0, "2"])
def test_a_malformed_call_count_is_malformed_and_closes(model_calls: Any) -> None:
    ledger = _ledger()
    with pytest.raises(InvalidChargeError):
        ledger.settle(ledger.authorize(_worst_case()), model_calls=model_calls)
    usage = ledger.usage()
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (3, 3_000, 1_500)
    assert ledger.closed_reason == "invalid_settlement"
    with pytest.raises(BudgetClosedError):
        ledger.authorize(BudgetRequest(sql_executions=1))


def test_a_call_count_cannot_be_settled_against_a_tool_reservation() -> None:
    ledger = _ledger()
    with pytest.raises(InvalidChargeError):
        ledger.settle(ledger.authorize(BudgetRequest(sql_executions=1)), model_calls=1)
    assert ledger.closed_reason == "invalid_settlement"


def test_policy_refusal_closes_the_ledger_permanently() -> None:
    ledger = _ledger()
    in_flight = ledger.authorize(BudgetRequest(retrievals=1))
    ledger.close("policy_refused")
    with pytest.raises(BudgetClosedError) as caught:
        ledger.authorize(BudgetRequest(retrievals=1))
    assert caught.value.reason == "policy_refused"
    assert ledger.settle(in_flight).retrievals == 1  # in-flight work still counts


def test_every_investigation_terminal_refusal_closes_the_ledger_with_a_known_reason() -> None:
    assert set(LEDGER_CLOSE_REASON) == set(LIFECYCLE_TARGET)
    assert set(LEDGER_CLOSE_REASON.values()) <= set(get_args(CloseReason))
    assert LEDGER_CLOSE_REASON[RefusalCode.CANCELLED] == "cancelled"
    assert LEDGER_CLOSE_REASON[RefusalCode.CAPABILITY_MISSING] == "policy_refused"


# --- provider-call meter ---------------------------------------------------------------------


def test_reports_are_unchanged_and_harmless_without_a_meter() -> None:
    tapped: list[tuple[int | None, int | None]] = []
    with tap_usage(lambda prompt, output: tapped.append((prompt, output))):
        report_usage(prompt_tokens=10, output_tokens=5, retries=2)
    report_fallbacks(1)
    assert tapped == [(10, 5)]


def test_the_meter_sees_usage_retries_and_fallbacks_only_inside_its_block() -> None:
    meter = RecordingMeter()
    with meter_provider_calls(meter):
        report_usage(prompt_tokens=10, output_tokens=None, retries=2)
        report_fallbacks(1)
    report_usage(prompt_tokens=1, output_tokens=1)
    report_fallbacks(3)
    assert meter.usage == [(10, None, 2)]
    assert meter.fallbacks == [1]


def test_meters_nest_and_restore() -> None:
    outer, inner = RecordingMeter(), RecordingMeter()
    with meter_provider_calls(outer):
        with meter_provider_calls(inner):
            report_usage(prompt_tokens=1, output_tokens=1)
        report_usage(prompt_tokens=2, output_tokens=2)
    assert inner.usage == [(1, 1, 0)]
    assert outer.usage == [(2, 2, 0)]


def test_a_meter_that_asks_for_it_learns_who_served_each_call() -> None:
    class ServedMeter(RecordingMeter):
        def __init__(self) -> None:
            super().__init__()
            self.served: list[tuple[str, str]] = []

        def record_served_by(self, provider: str, model: str) -> None:
            self.served.append((provider, model))

    served, plain = ServedMeter(), RecordingMeter()
    with meter_provider_calls(served):
        report_served_by("gemini", "gemini-2.5-pro")
    with meter_provider_calls(plain) as health:
        report_served_by("gemini", "gemini-2.5-pro")  # a meter without the hook is unaffected
    report_served_by("ollama", "llama3")  # outside any meter: ignored
    assert served.served == [("gemini", "gemini-2.5-pro")]
    assert health.faulted is False


def test_negative_or_absurd_reports_reach_the_meter_sanitised() -> None:
    meter = RecordingMeter()
    with meter_provider_calls(meter):
        report_usage(prompt_tokens=-5, output_tokens=True, retries=-3)
        report_fallbacks(-2)
    assert meter.usage == [(None, None, 0)]
    assert meter.fallbacks == [0]


class ExplodingMeter:
    """A meter whose recording fails with content in the message."""

    def record_usage(
        self, prompt_tokens: int | None, output_tokens: int | None, retries: int
    ) -> None:
        raise RuntimeError(f"meter down {CANARY}")

    def record_fallbacks(self, count: int) -> None:
        raise RuntimeError(f"meter down {CANARY}")


CANARY = "CANARY-select-secret-from-customers"


def test_a_failing_meter_marks_usage_unknown_without_breaking_reporting() -> None:
    tapped: list[tuple[int | None, int | None]] = []
    with meter_provider_calls(ExplodingMeter()) as health:
        assert health.faulted is False
        with tap_usage(lambda prompt, output: tapped.append((prompt, output))):
            report_usage(prompt_tokens=10, output_tokens=5)  # no exception escapes
        report_fallbacks(1)
    assert health.faulted is True
    assert tapped == [(10, 5)]  # ordinary observability still saw the report
    assert CANARY not in repr(vars(health))


def test_a_healthy_meter_is_not_marked_faulted() -> None:
    with meter_provider_calls(RecordingMeter()) as health:
        report_usage(prompt_tokens=1, output_tokens=1)
    assert health.faulted is False


def test_a_meter_fault_does_not_turn_a_successful_call_into_a_fallback() -> None:
    first, second = _Member("first", fail=False), _Member("second", fail=False)
    client = FallbackLLMClient([first, second])
    with meter_provider_calls(ExplodingMeter()) as health:
        response = client.generate("prompt")
    assert response.provider == "first"  # no extra provider call was made
    assert health.faulted is True


def test_a_failing_tap_cannot_hide_a_report_from_the_meter() -> None:
    meter = RecordingMeter()

    def broken_tap(prompt: int | None, output: int | None) -> None:
        raise RuntimeError("tap down")

    with meter_provider_calls(meter) as health, tap_usage(broken_tap):
        with pytest.raises(RuntimeError):
            report_usage(prompt_tokens=3, output_tokens=4)
    assert meter.usage == [(3, 4, 0)]
    assert health.faulted is False


def test_unknown_usage_after_a_meter_fault_settles_the_full_reservation_never_zero() -> None:
    # The S5 contract: a faulted meter means unknown usage, settled without counts.
    ledger = _ledger()
    reservation = ledger.authorize(_worst_case())
    with meter_provider_calls(ExplodingMeter()) as health:
        report_usage(prompt_tokens=1, output_tokens=1)
    assert health.faulted
    usage = ledger.settle(reservation)
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (3, 3_000, 1_500)
    assert usage.unmetered_model_calls == 1


class _Member:
    def __init__(self, provider: str, *, fail: bool, retries: int = 0) -> None:
        self.provider = provider
        self.model = f"{provider}-model"
        self.configured = True
        self._fail = fail
        self._retries = retries

    def generate(self, prompt: str) -> LLMResponse:
        if self._fail:
            raise LLMGenerationError("provider unavailable")
        report_usage(prompt_tokens=len(prompt), output_tokens=7, retries=self._retries)
        return LLMResponse(text="SELECT 1", model=self.model, provider=self.provider)


def test_the_meter_observes_a_fallback_chain_as_the_providers_report_it() -> None:
    client = FallbackLLMClient(
        [_Member("first", fail=True), _Member("second", fail=False, retries=1)]
    )
    meter = RecordingMeter()
    with meter_provider_calls(meter):
        client.generate("prompt")
    # The failed member reported nothing: the gateway must treat its calls as unknown (full charge).
    assert meter.usage == [(6, 7, 1)]
    assert meter.fallbacks == [1]
