"""PROD-1 S2: the bounded Responses adapter's create, poll, cancel, deadline, and usage (offline).

Every test scripts ``httpx.MockTransport`` and an injected clock whose ``sleep`` only advances the
clock. No API key, network, real sleep, or provider account is needed, and nothing here proves
live provider behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from packages.investigation.budget import EXECUTION_LIMITS, PLANNING_LIMITS, BudgetLedger
from packages.investigation.gateway import (
    PLANNING_STAGE_OUTPUT_TOKENS,
    MeteringUnbounded,
    ProviderAttemptBound,
    ProviderEnvelope,
)
from src.llm import openai_responses_bounded as bounded_module
from src.llm.base import LLMConfigurationError, LLMGenerationError
from src.llm.bounded import (
    DeadlineBoundedModelAdapter,
    DeadlineModelResult,
    DeadlineTermination,
    PlanningDeadline,
    admit_planning_adapter,
)
from src.llm.observability import meter_provider_calls
from src.llm.openai_responses import (
    ResponsesModelProfile,
    TokenCountError,
    TokenCountFailure,
)
from src.llm.openai_responses_bounded import (
    REVIEWED_CAPABILITY_PROFILES,
    AdapterFailure,
    BudgetDisposition,
    MeasuredUsage,
    OpenAIResponsesBoundedAdapter,
    ResponsesAdapterError,
    ResponsesCapabilityProfile,
)
from src.llm.responses_transport import (
    INPUT_TOKENS_PATH,
    MAX_TIMEOUT_SECONDS,
    Delivery,
    ResponsesTransport,
    valid_response_id,
)

# Deliberately not key-shaped: it cannot authenticate anywhere, and only mock transports see it.
FAKE_KEY = "offline-test-placeholder-not-a-credential"
SNAPSHOT = "test-model-2099-01-01"
RID = "resp_0123abcd"
PROMPT = "Explain churn for SEGMENT-SECRET-7731"
INSTRUCTIONS = "Follow the application task exactly."
PROVIDER_TEXT = "internal provider detail: account 4242"
ANSWER = '{"ok": true}'
CAP = PLANNING_STAGE_OUTPUT_TOKENS
USAGE = {"input_tokens": 42, "output_tokens": 17}
START = 1_000.0

CREATE = ("POST", "/responses")
COUNT = ("POST", INPUT_TOKENS_PATH)
POLL = ("GET", f"/responses/{RID}")
CANCEL = ("POST", f"/responses/{RID}/cancel")


class Clock:
    """Monotonic test clock; ``sleep`` advances it instead of waiting."""

    def __init__(self) -> None:
        self.now = START
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


type Step = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


class Provider:
    """Scripted OpenAI: answers each request in order and records what was asked, and when."""

    def __init__(self, clock: Clock, *steps: Step, then: Callable[[], Step] | None = None) -> None:
        self.clock = clock
        self.steps = list(steps)
        self.then = then
        self.calls: list[tuple[str, str, float, float]] = []  # method, path, time, read timeout
        self.bodies: list[bytes] = []
        self.hosts: set[str] = set()
        self.unscripted = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        timeout = request.extensions.get("timeout", {})
        path = request.url.path.removeprefix("/v1")
        self.calls.append((request.method, path, self.clock(), float(timeout.get("read", 0))))
        self.bodies.append(request.content)
        self.hosts.add(f"{request.url.scheme}://{request.url.host}")
        if self.steps:
            step = self.steps.pop(0)
        elif self.then is not None:
            step = self.then()  # a fresh response each time
        else:
            self.unscripted = True
            raise AssertionError("unscripted request")
        if isinstance(step, Exception):
            raise step
        if isinstance(step, httpx.Response):
            return step
        return step(request)

    @property
    def paths(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, *_ in self.calls]


class Token:
    """A cancellation token that turns on when ``when`` first holds (or never)."""

    def __init__(self, when: Callable[[], bool] = lambda: False) -> None:
        self._when = when

    @property
    def cancel_requested(self) -> bool:
        return self._when()


def at(clock: Clock, moment: float) -> Token:
    return Token(lambda: clock() >= moment)


def after_create(provider: Provider) -> Token:
    return Token(lambda: CREATE in provider.paths)


class Meter:
    def __init__(self) -> None:
        self.usage: list[tuple[int | None, int | None, int]] = []
        self.served: list[tuple[str, str]] = []
        self.fallbacks = 0

    def record_usage(self, prompt: int | None, output: int | None, retries: int) -> None:
        self.usage.append((prompt, output, retries))

    def record_fallbacks(self, count: int) -> None:
        self.fallbacks += count

    def record_served_by(self, provider: str, model: str) -> None:
        self.served.append((provider, model))


def counted(tokens: object = 42) -> httpx.Response:
    return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": tokens})


def document(
    status: object,
    *,
    rid: object = RID,
    model: object = SNAPSHOT,
    usage: dict[str, Any] | None = None,
    output: list[Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "object": "response",
        "id": rid,
        "model": model,
        "status": status,
        "background": True,
        **extra,
    }
    if output is not None:
        body["output"] = output
    if usage is not None:
        body["usage"] = usage
    return body


def message(text: str = ANSWER) -> list[Any]:
    return [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
    ]


def ok(status: object, **kwargs: Any) -> httpx.Response:
    return httpx.Response(200, json=document(status, **kwargs))


def queued() -> httpx.Response:
    return ok("queued")


def running() -> httpx.Response:
    return ok("in_progress")


def done(usage: dict[str, Any] | None = USAGE, **kwargs: Any) -> httpx.Response:
    return ok("completed", usage=usage, output=message(), **kwargs)


def cancelled(usage: dict[str, Any] | None = None) -> httpx.Response:
    return ok("cancelled", usage=usage)


def capabilities(**changes: Any) -> ResponsesCapabilityProfile:
    values: dict[str, Any] = {
        "profile": ResponsesModelProfile(model=SNAPSHOT, max_output_tokens=4_000),
        "max_input_tokens": 6_000,
        "exact_input_count": True,
        "background_mode": True,
        "provider_cancel": True,
        "usage_on_completion": True,
        "review": "test-fixture-1",
    }
    values.update(changes)
    return ResponsesCapabilityProfile(**values)


def make(
    provider: Provider, *, key: str | None = FAKE_KEY, **kwargs: Any
) -> OpenAIResponsesBoundedAdapter:
    clock = provider.clock
    transport = ResponsesTransport(key, transport=httpx.MockTransport(provider), clock=clock)
    options: dict[str, Any] = {
        "instructions": INSTRUCTIONS,
        "max_output_tokens": CAP,
        "poll_interval_seconds": 1.0,
        "max_polls": 10,
        "cancel_reserve_seconds": 2.0,
        "clock": clock,
        "sleep": clock.sleep,
    }
    options.update(kwargs)
    profile = options.pop("capabilities", capabilities())
    return OpenAIResponsesBoundedAdapter(profile, transport=transport, **options)


def call(
    adapter: OpenAIResponsesBoundedAdapter,
    *,
    seconds: float = 30.0,
    token: Token | None = None,
    prompt: str = PROMPT,
    meter: Meter | None = None,
    count: bool = True,
) -> DeadlineModelResult:
    if count:
        adapter.count_input_tokens(prompt)
    with meter_provider_calls(meter or Meter()):
        return adapter.generate_once_with_deadline(
            prompt,
            max_output_tokens=CAP,
            deadline=PlanningDeadline(seconds),
            cancellation=token or Token(),
        )


def fails(
    adapter: OpenAIResponsesBoundedAdapter, failure: AdapterFailure, **kwargs: Any
) -> ResponsesAdapterError:
    with pytest.raises(ResponsesAdapterError) as caught:
        call(adapter, **kwargs)
    assert caught.value.failure is failure
    return caught.value


def assert_within(provider: Provider, hard_stop: float) -> None:
    """No generation request started after, or could run past, the hard stop.

    These adapter tests count with ``count_input_tokens`` before the call's deadline exists, so
    the count is bounded by the adapter's fixed count timeout here. The gateways count with
    ``count_input_tokens_within`` (PROD-1 S3), which bounds it by the same deadline; see
    ``tests/test_prod1_gateway_integration.py``.
    """
    for _, path, at, read in provider.calls:
        if path != INPUT_TOKENS_PATH:
            assert at + read * 5 <= hard_stop + 1e-9


# --- 1-6: creation ------------------------------------------------------------------------


def test_a_valid_background_create_completes_with_one_create_and_one_report() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), running(), done())
    adapter = make(provider)
    meter = Meter()
    result = call(adapter, meter=meter)

    assert provider.paths == [COUNT, CREATE, POLL, POLL]
    assert provider.bodies[2:] == [b"", b""]  # polls carry no body
    sent_count, create = json.loads(provider.bodies[0]), json.loads(provider.bodies[1])
    assert create == sent_count | {"max_output_tokens": CAP, "store": False, "background": True}
    assert create["background"] is True and create["store"] is False
    assert create["max_output_tokens"] == CAP and create["model"] == SNAPSHOT
    assert result.termination is DeadlineTermination.COMPLETED
    assert result.termination_confirmed and result.provider_execution_bound_enforced
    assert result.response is not None and result.response.text == ANSWER
    assert (result.response.provider, result.response.model) == ("openai", SNAPSHOT)
    assert meter.usage == [(42, 17, 0)] and meter.served == [("openai", SNAPSHOT)]
    assert adapter.last_outcome is not None
    assert adapter.last_outcome.budget is BudgetDisposition.SETTLE_MEASURED
    assert adapter.last_outcome.usage == MeasuredUsage(42, 17)


def test_no_create_without_a_valid_exact_count() -> None:
    clock = Clock()
    provider = Provider(clock)
    adapter = make(provider)
    fails(adapter, AdapterFailure.COUNT_REQUIRED, count=False)
    assert provider.calls == []

    provider = Provider(clock, counted(tokens=0))
    adapter = make(provider)
    with pytest.raises(TokenCountError):
        adapter.count_input_tokens(PROMPT)
    fails(adapter, AdapterFailure.COUNT_REQUIRED, count=False)
    assert provider.paths == [COUNT]


def test_a_count_authorizes_at_most_one_create() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), done())
    adapter = make(provider)
    call(adapter)
    fails(adapter, AdapterFailure.COUNT_REQUIRED, count=False)
    assert provider.paths == [COUNT, CREATE]


def test_a_count_for_a_different_prompt_is_refused_before_create() -> None:
    clock = Clock()
    provider = Provider(clock, counted())
    adapter = make(provider)
    adapter.count_input_tokens("a different prompt")
    with pytest.raises(ResponsesAdapterError) as caught, meter_provider_calls(Meter()):
        adapter.generate_once_with_deadline(
            PROMPT, max_output_tokens=CAP, deadline=PlanningDeadline(30), cancellation=Token()
        )
    assert caught.value.failure is AdapterFailure.REQUEST_CHANGED
    assert caught.value.delivery is Delivery.NOT_SENT
    assert provider.paths == [COUNT]


def test_input_over_the_budget_is_refused_at_count() -> None:
    clock = Clock()
    provider = Provider(clock, counted(tokens=6_001))
    adapter = make(provider)
    with pytest.raises(TokenCountError) as caught:
        adapter.count_input_tokens(PROMPT)
    assert caught.value.failure is TokenCountFailure.INPUT_OVER_BOUND
    fails(adapter, AdapterFailure.COUNT_REQUIRED, count=False)
    assert provider.paths == [COUNT]


def test_a_create_that_was_never_sent_releases_and_may_be_retried_by_the_caller() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), httpx.ConnectError("refused"), counted(), done())
    adapter = make(provider)
    error = fails(adapter, AdapterFailure.CREATE_NOT_SENT)
    assert error.delivery is Delivery.NOT_SENT
    assert adapter.last_outcome is not None
    assert adapter.last_outcome.budget is BudgetDisposition.RELEASE
    assert not adapter.halted
    assert call(adapter).termination is DeadlineTermination.COMPLETED


@pytest.mark.parametrize(
    ("step", "failure", "delivery"),
    [
        (httpx.ReadTimeout("slow"), AdapterFailure.CREATE_OUTCOME_UNKNOWN, Delivery.UNKNOWN),
        (
            httpx.RemoteProtocolError("reset"),
            AdapterFailure.CREATE_OUTCOME_UNKNOWN,
            Delivery.UNKNOWN,
        ),
        (
            httpx.Response(500, text=PROVIDER_TEXT),
            AdapterFailure.CREATE_REJECTED,
            Delivery.RECEIVED,
        ),
        (httpx.Response(429), AdapterFailure.CREATE_REJECTED, Delivery.RECEIVED),
    ],
)
def test_an_uncertain_create_is_never_repeated_and_halts_the_adapter(
    step: Step, failure: AdapterFailure, delivery: Delivery
) -> None:
    clock = Clock()
    provider = Provider(clock, counted(), step)
    adapter = make(provider)
    meter = Meter()
    error = fails(adapter, failure, meter=meter)
    assert error.delivery is delivery
    assert adapter.halted and meter.usage == [] and meter.served == []
    assert adapter.last_outcome is not None
    assert adapter.last_outcome.budget is BudgetDisposition.HOLD_RESERVATION
    # No further count or create, ever, on this instance.
    with pytest.raises(ResponsesAdapterError) as caught:
        adapter.count_input_tokens(PROMPT)
    assert caught.value.failure is AdapterFailure.HALTED
    fails(adapter, AdapterFailure.HALTED, count=False)
    assert provider.paths == [COUNT, CREATE]


# --- 7-11: polling and terminal states ----------------------------------------------------


def test_polling_is_bounded_and_sleeps_only_through_the_injected_clock() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), queued(), running(), running(), done())
    adapter = make(provider)
    result = call(adapter)
    assert result.termination is DeadlineTermination.COMPLETED
    assert provider.paths == [COUNT, CREATE, POLL, POLL, POLL, POLL]
    assert clock.sleeps == [1.0, 1.0, 1.0, 1.0]
    assert_within(provider, START + 30)


def test_poll_exhaustion_cancels_and_fails_closed() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), running(), running(), cancelled())
    adapter = make(provider, max_polls=2)
    error = fails(adapter, AdapterFailure.POLL_EXHAUSTED)
    assert error.delivery is Delivery.RECEIVED
    assert provider.paths == [COUNT, CREATE, POLL, POLL, CANCEL]
    assert adapter.last_outcome is not None and adapter.last_outcome.termination_confirmed
    assert adapter.last_outcome.budget is BudgetDisposition.HOLD_RESERVATION  # no usage
    assert not adapter.halted  # termination was confirmed


def test_the_deadline_stops_polling_with_room_to_cancel() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), running(), running(), cancelled())
    adapter = make(provider)
    result = call(adapter, seconds=5.0)
    assert provider.paths == [COUNT, CREATE, POLL, POLL, CANCEL]
    assert result.termination is DeadlineTermination.PROVIDER_DEADLINE_ENFORCED
    assert result.termination_confirmed and result.provider_execution_bound_enforced
    assert not result.cancellation_requested and result.response is None
    assert_within(provider, START + 5)
    cancel_at = provider.calls[-1][2]
    assert cancel_at <= START + 5 - 2.0  # the cancel reserve is kept


@pytest.mark.parametrize(
    "bad",
    [
        ok("weird_status"),
        ok(None),
        ok(7),
        ok("queued", rid="resp_someoneelse"),
        httpx.Response(200, json={"object": "list", "id": RID, "status": "completed"}),
    ],
)
def test_unknown_or_malformed_states_are_never_success(bad: httpx.Response) -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), bad, cancelled())
    adapter = make(provider)
    with pytest.raises(ResponsesAdapterError) as caught:
        call(adapter)
    assert caught.value.failure in {AdapterFailure.UNKNOWN_STATE, AdapterFailure.MALFORMED_RESPONSE}
    assert provider.paths == [COUNT, CREATE, POLL, CANCEL]  # only the known ID is cancelled


def test_a_backward_state_transition_is_unknown_state() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), running(), queued(), cancelled())
    fails(make(provider), AdapterFailure.UNKNOWN_STATE)


def test_a_late_completed_document_after_an_unknown_state_is_still_not_success() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), ok("mystery"), done())
    adapter = make(provider)
    meter = Meter()
    fails(adapter, AdapterFailure.UNKNOWN_STATE, meter=meter)
    assert meter.served == []  # no completed identity is reported
    assert provider.paths == [COUNT, CREATE, POLL, CANCEL]


def test_a_create_without_an_addressable_id_halts_without_more_requests() -> None:
    for rid in ("resp_x/../y", "resp_x?y=1", "resp_", "other_123", 42, "resp_abc\n"):
        clock = Clock()
        provider = Provider(clock, counted(), ok("queued", rid=rid))
        adapter = make(provider)
        fails(adapter, AdapterFailure.MALFORMED_RESPONSE)
        assert adapter.halted and provider.paths == [COUNT, CREATE]
        assert adapter.last_outcome is not None
        assert adapter.last_outcome.budget is BudgetDisposition.HOLD_RESERVATION


def test_terminal_completion_shapes() -> None:
    clock = Clock()
    capped = ok(
        "incomplete",
        usage={"input_tokens": 42, "output_tokens": CAP},
        output=message('{"partial"'),
        incomplete_details={"reason": "max_output_tokens"},
    )
    result = call(make(Provider(clock, counted(), capped)))
    assert result.termination is DeadlineTermination.COMPLETED
    assert result.provider_execution_bound_enforced  # truncated at, not above, the cap

    filtered = ok(
        "incomplete", usage=USAGE, output=message(), incomplete_details={"reason": "content_filter"}
    )
    fails(make(Provider(clock, counted(), filtered)), AdapterFailure.PROVIDER_INCOMPLETE)
    fails(make(Provider(clock, counted(), ok("failed"))), AdapterFailure.PROVIDER_FAILED)

    over = done(usage={"input_tokens": 42, "output_tokens": CAP + 1})
    assert not call(make(Provider(clock, counted(), over))).provider_execution_bound_enforced


@pytest.mark.parametrize(
    "output",
    [
        [{"type": "function_call", "name": "run_sql", "arguments": "{}"}],
        [{"type": "message", "role": "assistant", "content": [{"type": "refusal"}]}],
        [{"type": "message", "role": "user", "content": []}],
        [{"type": "reasoning", "summary": []}, *message()],  # non-reasoning profile
        "not a list",
    ],
)
def test_unexpected_output_items_are_malformed(output: Any) -> None:
    clock = Clock()
    provider = Provider(clock, counted(), ok("completed", usage=USAGE, output=output))
    meter = Meter()
    fails(make(provider), AdapterFailure.MALFORMED_RESPONSE, meter=meter)
    assert meter.served == []


def test_a_reasoning_profile_skips_reasoning_items() -> None:
    clock = Clock()
    profile = capabilities(
        profile=ResponsesModelProfile(
            model=SNAPSHOT, max_output_tokens=4_000, reasoning_efforts=frozenset({"low"})
        )
    )
    reply = ok("completed", usage=USAGE, output=[{"type": "reasoning", "summary": []}, *message()])
    adapter = make(Provider(clock, counted(), reply), capabilities=profile, reasoning_effort="low")
    result = call(adapter)
    assert result.response is not None and result.response.text == ANSWER


def test_a_response_from_another_model_is_reported_and_refused() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), done(model="other-model-2099-01-01"))
    meter = Meter()
    fails(make(provider), AdapterFailure.IDENTITY_MISMATCH, meter=meter)
    assert meter.served == [("openai", "other-model-2099-01-01")]  # the gateway sees it


# --- 12-16: cancellation ------------------------------------------------------------------


def test_a_provider_confirmed_cancellation() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), running(), cancelled())
    adapter = make(provider)
    result = call(adapter, token=at(clock, START + 1))
    assert result.termination is DeadlineTermination.PROVIDER_CANCELLED
    assert result.cancellation_requested and result.termination_confirmed
    assert provider.paths == [COUNT, CREATE, POLL, CANCEL]


def test_cancellation_before_create_sends_nothing() -> None:
    clock = Clock()
    provider = Provider(clock, counted())
    adapter = make(provider)
    fails(adapter, AdapterFailure.CANCELLED_BEFORE_SEND, token=at(clock, START))
    assert provider.paths == [COUNT]


def test_an_unreadable_token_is_treated_as_a_cancellation() -> None:
    class Broken:
        @property
        def cancel_requested(self) -> bool:
            raise RuntimeError("token store down")

    clock = Clock()
    provider = Provider(clock, counted())
    adapter = make(provider)
    adapter.count_input_tokens(PROMPT)
    with pytest.raises(ResponsesAdapterError) as caught:
        adapter.generate_once_with_deadline(
            PROMPT, max_output_tokens=CAP, deadline=PlanningDeadline(30), cancellation=Broken()
        )
    assert caught.value.failure is AdapterFailure.CANCELLED_BEFORE_SEND


def test_a_cancel_that_is_never_confirmed_is_reported_unconfirmed_and_halts() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), running(), then=running)
    adapter = make(provider)
    meter = Meter()
    result = call(adapter, seconds=6.0, token=at(clock, START + 1), meter=meter)
    assert result.termination is DeadlineTermination.PROVIDER_CANCELLED
    assert not result.termination_confirmed and not result.provider_execution_bound_enforced
    assert adapter.halted and meter.usage == []
    assert adapter.last_outcome is not None
    assert adapter.last_outcome.budget is BudgetDisposition.HOLD_RESERVATION
    assert provider.paths[:4] == [COUNT, CREATE, POLL, CANCEL]
    assert set(provider.paths[2:]) <= {POLL, CANCEL}
    assert_within(provider, START + 6)


def test_cancel_timeouts_end_unconfirmed_without_treating_the_timeout_as_a_stop() -> None:
    clock = Clock()
    timeout = httpx.ReadTimeout("slow")
    provider = Provider(
        clock, counted(), queued(), running(), running(), timeout, timeout, then=running
    )
    adapter = make(provider)
    result = call(adapter, seconds=5.0)
    assert result.termination is DeadlineTermination.PROVIDER_DEADLINE_ENFORCED
    assert not result.termination_confirmed
    assert provider.paths[2:6] == [POLL, POLL, CANCEL, CANCEL]  # max_cancel_attempts=2
    assert adapter.halted
    assert_within(provider, START + 5)


def test_a_repeated_cancel_after_a_timeout_is_idempotent_and_confirmed() -> None:
    clock = Clock()
    provider = Provider(
        clock, counted(), queued(), httpx.ReadTimeout("slow"), cancelled(), cancelled()
    )
    adapter = make(provider, max_cancel_attempts=3)
    result = call(adapter, token=after_create(provider))
    assert result.termination is DeadlineTermination.PROVIDER_CANCELLED
    assert result.termination_confirmed
    assert provider.paths == [COUNT, CREATE, CANCEL, CANCEL]  # stops at the first confirmation
    assert not adapter.halted


def test_a_refused_cancel_is_resolved_by_reading_the_provider_state() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), httpx.Response(400, text=PROVIDER_TEXT), done())
    meter = Meter()
    result = call(make(provider), token=after_create(provider), meter=meter)
    # It finished first: a completed answer to a cancelled call, which the gateway discards.
    assert result.termination is DeadlineTermination.COMPLETED and result.cancellation_requested
    assert provider.paths == [COUNT, CREATE, CANCEL, POLL]


def test_a_transient_poll_failure_after_cancel_does_not_hide_a_confirmed_cancellation() -> None:
    # Regression: the fault list used to turn a confirmed cancellation into ``poll_failed``.
    clock = Clock()
    provider = Provider(
        clock, counted(), queued(), running(), httpx.ReadTimeout("slow"), cancelled()
    )
    result = call(make(provider), token=after_create(provider))
    assert result.termination is DeadlineTermination.PROVIDER_CANCELLED
    assert result.termination_confirmed and result.cancellation_requested
    assert provider.paths == [COUNT, CREATE, CANCEL, POLL, POLL]


def test_an_unrequested_provider_cancellation_is_returned_for_the_gateway_to_refuse() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), cancelled())
    result = call(make(provider))
    assert result.termination is DeadlineTermination.PROVIDER_CANCELLED
    assert result.termination_confirmed and not result.cancellation_requested


def test_cancellation_without_usage_reports_nothing_and_holds_the_reservation() -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), cancelled(usage=None))
    adapter = make(provider)
    meter = Meter()
    call(adapter, token=after_create(provider), meter=meter)
    assert meter.usage == [] and meter.served == []
    assert adapter.last_outcome is not None
    assert adapter.last_outcome.usage is None
    assert adapter.last_outcome.usage_status == "unknown"
    assert adapter.last_outcome.budget is BudgetDisposition.HOLD_RESERVATION


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": 42},
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": 1.5, "output_tokens": 1},
    ],
)
def test_missing_or_malformed_usage_is_unknown_never_estimated(usage: Any) -> None:
    clock = Clock()
    provider = Provider(clock, counted(), ok("completed", usage=usage, output=message()))
    adapter = make(provider)
    meter = Meter()
    result = call(adapter, meter=meter)
    assert meter.usage == []
    assert not result.provider_execution_bound_enforced  # the gateway refuses it
    assert adapter.last_outcome is not None
    assert adapter.last_outcome.budget is BudgetDisposition.HOLD_RESERVATION


# --- 17-18: usage and budget --------------------------------------------------------------


def test_measured_usage_is_reported_verbatim_even_when_it_differs_from_the_count() -> None:
    clock = Clock()
    provider = Provider(clock, counted(42), done(usage={"input_tokens": 45, "output_tokens": 9}))
    meter = Meter()
    adapter = make(provider)
    call(adapter, meter=meter)
    assert meter.usage == [(45, 9, 0)]  # not corrected to 42: the gateway sees the mismatch
    assert adapter.last_outcome is not None and adapter.last_outcome.counted_input_tokens == 42


def test_cancelled_usage_when_reported_is_measured() -> None:
    clock = Clock()
    usage = {"input_tokens": 42, "output_tokens": 3}
    provider = Provider(clock, counted(), queued(), cancelled(usage=usage))
    adapter = make(provider)
    meter = Meter()
    call(adapter, token=after_create(provider), meter=meter)
    assert meter.usage == [(42, 3, 0)]
    assert adapter.last_outcome is not None
    assert adapter.last_outcome.budget is BudgetDisposition.SETTLE_MEASURED


def test_reservations_are_released_only_when_the_create_provably_never_left() -> None:
    def outcome(*steps: Step, **kwargs: Any) -> BudgetDisposition:
        clock = Clock()
        adapter = make(Provider(clock, counted(), *steps, then=running))
        try:
            call(adapter, **kwargs)
        except LLMGenerationError:
            pass
        assert adapter.last_outcome is not None
        return adapter.last_outcome.budget

    assert outcome(httpx.ConnectError("x")) is BudgetDisposition.RELEASE
    assert outcome(httpx.ReadTimeout("x")) is BudgetDisposition.HOLD_RESERVATION
    assert outcome(httpx.Response(500)) is BudgetDisposition.HOLD_RESERVATION
    assert outcome(queued(), seconds=5.0) is BudgetDisposition.HOLD_RESERVATION  # unconfirmed
    assert outcome(done(usage=None)) is BudgetDisposition.HOLD_RESERVATION
    assert outcome(done()) is BudgetDisposition.SETTLE_MEASURED


def test_the_adapter_holds_no_prices() -> None:
    source = Path(bounded_module.__file__).read_text(encoding="utf-8").lower()
    assert "pricing" not in source and "usd" not in source and "cost" not in source


# --- 19-20: deadlines ---------------------------------------------------------------------


def test_execution_receives_the_remaining_ledger_deadline() -> None:
    clock = Clock()
    ledger = BudgetLedger(EXECUTION_LIMITS, clock=clock)
    provider = Provider(clock, counted(), queued(), then=running)
    adapter = make(provider)
    adapter.count_input_tokens(PROMPT)
    clock.now += 236.0  # four seconds of the 240-second execution ledger remain
    # As the gateways do: read the ledger after the count, immediately before the call.
    deadline = PlanningDeadline(ledger.remaining_wall_clock())
    assert deadline.remaining_seconds == pytest.approx(4.0)
    started = clock()
    with meter_provider_calls(Meter()):
        result = adapter.generate_once_with_deadline(
            PROMPT, max_output_tokens=CAP, deadline=deadline, cancellation=Token()
        )
    assert result.termination is DeadlineTermination.PROVIDER_DEADLINE_ENFORCED
    assert not result.termination_confirmed
    assert_within(provider, started + 4.0)
    assert all(read * 5 <= 4.0 for _, path, _, read in provider.calls if path != INPUT_TOKENS_PATH)


def test_generate_once_without_a_deadline_is_refused_before_any_io() -> None:
    clock = Clock()
    provider = Provider(clock, counted())
    adapter = make(provider)
    adapter.count_input_tokens(PROMPT)
    with pytest.raises(ResponsesAdapterError) as caught:
        adapter.generate_once(PROMPT, max_output_tokens=CAP)
    assert caught.value.failure is AdapterFailure.DEADLINE_REQUIRED
    assert caught.value.delivery is Delivery.NOT_SENT
    assert provider.paths == [COUNT]
    fails(adapter, AdapterFailure.COUNT_REQUIRED, count=False)  # the count was consumed


def test_a_deadline_too_short_to_cancel_sends_nothing() -> None:
    clock = Clock()
    provider = Provider(clock, counted())
    adapter = make(provider)
    fails(adapter, AdapterFailure.DEADLINE_BEFORE_SEND, seconds=2.0)  # == the cancel reserve
    assert provider.paths == [COUNT]


def test_the_planning_ledger_stays_thirty_seconds_and_is_never_extended() -> None:
    assert PLANNING_LIMITS.wall_clock_seconds == 30.0
    assert EXECUTION_LIMITS.wall_clock_seconds == 240.0
    clock = Clock()
    ledger = BudgetLedger(PLANNING_LIMITS, clock=clock)
    provider = Provider(clock, counted(), queued(), then=running)
    adapter = make(provider, max_polls=600)
    adapter.count_input_tokens(PROMPT)
    with meter_provider_calls(Meter()):
        result = adapter.generate_once_with_deadline(
            PROMPT,
            max_output_tokens=CAP,
            deadline=PlanningDeadline(ledger.remaining_wall_clock()),
            cancellation=Token(),
        )
    assert result.termination is DeadlineTermination.PROVIDER_DEADLINE_ENFORCED
    assert_within(provider, START + 30.0)
    assert clock() <= START + 30.0
    assert all(timeout <= MAX_TIMEOUT_SECONDS / 5 for *_, timeout in provider.calls)


def test_the_output_cap_must_match_the_bound() -> None:
    clock = Clock()
    provider = Provider(clock, counted())
    adapter = make(provider)
    adapter.count_input_tokens(PROMPT)
    with pytest.raises(ResponsesAdapterError) as caught:
        adapter.generate_once_with_deadline(
            PROMPT, max_output_tokens=CAP + 1, deadline=PlanningDeadline(30), cancellation=Token()
        )
    assert caught.value.failure is AdapterFailure.OUTPUT_CAP_MISMATCH
    assert provider.paths == [COUNT]


# --- 21-22: endpoints, IDs, redirects, sizes ----------------------------------------------


@pytest.mark.parametrize(
    "rid",
    [
        "resp_a/../b",
        "resp_a?x=1",
        "resp_a#f",
        "resp_%2e",
        "resp_a/cancel",
        "resp_",
        "resp_a\n",
        "../responses",
        "resp_ａ",
        "",
        None,
        7,
    ],
)
def test_response_ids_cannot_inject_paths(rid: Any) -> None:
    clock = Clock()
    provider = Provider(clock)
    transport = ResponsesTransport(FAKE_KEY, transport=httpx.MockTransport(provider), clock=clock)
    assert not valid_response_id(rid)
    for method in (transport.retrieve_response, transport.cancel_response):
        with pytest.raises(ValueError):
            method(rid, timeout_seconds=1.0)
    assert provider.calls == []


def test_the_transport_reaches_only_the_four_fixed_operations() -> None:
    clock = Clock()
    provider = Provider(clock, then=queued)
    transport = ResponsesTransport(FAKE_KEY, transport=httpx.MockTransport(provider), clock=clock)
    transport.create_response({"model": SNAPSHOT}, timeout_seconds=1.0)
    transport.retrieve_response(RID, timeout_seconds=1.0)
    transport.cancel_response(RID, timeout_seconds=1.0)
    with pytest.raises(ValueError):
        transport.post_json("/responses", {}, timeout_seconds=1.0)
    with pytest.raises(ValueError):
        transport.post_json(f"/responses/{RID}/cancel", {}, timeout_seconds=1.0)
    assert provider.paths == [CREATE, POLL, CANCEL]
    assert provider.hosts == {"https://api.openai.com"}
    assert provider.bodies[2] == b""  # cancel sends no body


def test_each_phase_gets_a_fifth_of_the_request_timeout() -> None:
    seen: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=document("queued"))

    transport = ResponsesTransport(FAKE_KEY, transport=httpx.MockTransport(handler))
    transport.retrieve_response(RID, timeout_seconds=10.0)
    assert seen == [{"connect": 2.0, "read": 2.0, "write": 2.0, "pool": 2.0}]


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_on_create_are_refused_and_never_followed(status: int) -> None:
    clock = Clock()
    redirect = httpx.Response(status, headers={"location": "https://example.invalid/v1/responses"})
    provider = Provider(clock, counted(), redirect)
    adapter = make(provider)
    fails(adapter, AdapterFailure.CREATE_REJECTED)
    assert provider.paths == [COUNT, CREATE] and adapter.halted


def test_a_redirected_or_oversized_poll_is_a_poll_failure_that_cancels() -> None:
    clock = Clock()
    huge = httpx.Response(200, content=b'{"pad":"' + b"x" * 70_000 + b'"}')
    provider = Provider(clock, counted(), queued(), huge, cancelled())
    fails(make(provider), AdapterFailure.POLL_FAILED)
    assert provider.paths == [COUNT, CREATE, POLL, CANCEL]

    redirect = httpx.Response(307, headers={"location": "https://example.invalid/"})
    provider = Provider(clock, counted(), queued(), redirect, cancelled())
    fails(make(provider), AdapterFailure.POLL_FAILED)
    assert all(path.startswith("/responses") for _, path in provider.paths)


# --- 23-26: secrets, network, credentials, admission --------------------------------------


def test_errors_reprs_and_outcomes_carry_no_content_or_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    provider = Provider(clock, counted(), queued(), ok("failed", error={"message": PROVIDER_TEXT}))
    adapter = make(provider)
    error = fails(adapter, AdapterFailure.PROVIDER_FAILED)
    rendered = " ".join(
        [str(error), repr(error), repr(adapter), repr(adapter.last_outcome), caplog.text]
    )
    for secret in (FAKE_KEY, PROMPT, INSTRUCTIONS, PROVIDER_TEXT, RID, ANSWER):
        assert secret not in rendered
    assert error.__cause__ is None
    assert str(error) == "provider_failed"


def test_a_completed_call_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    call(make(Provider(clock, counted(), done())))
    assert caplog.records == []


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    attempts: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        attempts.append(args)
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield attempts


def test_a_full_lifecycle_opens_no_socket(no_sockets: list[object]) -> None:
    clock = Clock()
    provider = Provider(clock, counted(), queued(), running(), cancelled())
    call(make(provider), token=at(clock, START + 1))
    assert no_sockets == [] and not provider.unscripted


def test_the_module_imports_in_a_clean_interpreter_without_keys_or_network() -> None:
    root = str(Path(__file__).resolve().parents[1])
    code = "\n".join(
        [
            "import socket, sys",
            "sys.path.insert(0, sys.argv[1])",
            "def refuse(*args, **kwargs):",
            "    raise SystemExit('network access attempted')",
            "socket.socket.connect = socket.create_connection = socket.getaddrinfo = refuse",
            "import src.llm.openai_responses_bounded as b",
            "assert dict(b.REVIEWED_CAPABILITY_PROFILES) == {}",
        ]
    )
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "PATH") if key in os.environ}
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, root],
        capture_output=True,
        text=True,
        env=env,  # no provider keys, no dotenv
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]


def test_construction_needs_a_configured_transport_but_no_real_credential() -> None:
    clock = Clock()
    provider = Provider(clock)
    with pytest.raises(LLMConfigurationError):
        make(provider, key=None)
    make(provider)  # the placeholder is enough offline
    assert provider.calls == []


@pytest.mark.parametrize(
    "missing",
    ["exact_input_count", "background_mode", "provider_cancel", "usage_on_completion"],
)
def test_a_profile_without_every_capability_cannot_be_built(missing: str) -> None:
    # Regression: ``Literal[True]`` alone accepted ``1``, which equals and hashes like ``True``.
    for value in (False, None, 1, 1.0, "true"):
        with pytest.raises(ValueError):
            capabilities(**{missing: value})
    values = dict(capabilities())
    del values[missing]
    with pytest.raises(ValueError):
        ResponsesCapabilityProfile(**values)


def test_a_bypassed_profile_is_revalidated_at_construction() -> None:
    forged = ResponsesCapabilityProfile.model_construct(
        **(dict(capabilities()) | {"provider_cancel": False})
    )
    with pytest.raises(ValueError):
        make(Provider(Clock()), capabilities=forged)


def test_admission_still_fails_closed_without_reviewed_evidence() -> None:
    assert dict(REVIEWED_CAPABILITY_PROFILES) == {}
    adapter = make(Provider(Clock()))
    assert isinstance(adapter, DeadlineBoundedModelAdapter)
    assert adapter.bound == ProviderAttemptBound(
        provider="openai",
        model=SNAPSHOT,
        data_recipient="OpenAI",
        max_attempts=1,
        max_output_tokens=CAP,
        input_bounded=True,
    )
    envelope = ProviderEnvelope(members=(adapter.bound,))
    with pytest.raises(MeteringUnbounded):
        admit_planning_adapter(adapter, envelope=envelope, capability=None)


def test_only_the_gated_admission_module_imports_the_adapter() -> None:
    root = Path(__file__).resolve().parents[1]
    importers = [
        path.relative_to(root).as_posix()
        for folder in ("src", "apps", "packages", "config")
        for path in (root / folder).rglob("*.py")
        if "openai_responses_bounded" in path.read_text(encoding="utf-8")
        and path.name != "openai_responses_bounded.py"
    ]
    assert sorted(importers) == [
        "apps/api/investigation_providers.py",
        "apps/api/provider_evidence.py",
    ]
    from apps.api.investigations import FailClosedAdapters

    assert FailClosedAdapters().planning().adapter is None
