"""EVAL-3 (#148): scripted rate-limit, backoff, and concurrency evidence, offline.

Each test closes one gap listed in ``docs/agents/briefs/148-eval3-concurrency-rate-limits.md``;
existing evidence is referenced there and in ``docs/operations/concurrency-and-rate-limits.md``
rather than repeated. Sleeps are recorded, never slept; no test contacts a provider.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors

from apps.api.rate_limit import InMemoryRateLimiter
from packages.investigation.budget import (
    EXECUTION_LIMITS,
    BudgetClosedError,
    BudgetExhaustedError,
    BudgetLedger,
    BudgetRequest,
)
from src.llm.base import LLMGenerationError, LLMResponse
from src.llm.fallback import FallbackLLMClient
from src.llm.gemini_client import GeminiClient, _is_retryable_error
from src.llm.observability import (
    LLMCallRecord,
    ObservedLLMClient,
    collect_question_usage,
    meter_provider_calls,
    report_usage,
)
from src.llm.providers import AnthropicClient, ModelTiers

BACKOFF = [0.25, 0.5]  # 0.25 * 2**attempt for the two retries


def _api_error(code: int, message: str, status: str) -> genai_errors.APIError:
    return genai_errors.APIError(
        code, {"error": {"code": code, "message": message, "status": status}}
    )


@dataclass
class _Models:
    """Scripted ``client.models``: raises the queued errors, then answers."""

    errors: list[Exception]
    calls: int = 0

    def generate_content(self, **_: Any) -> Any:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(
            text="answer",
            usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=6),
        )


def _gemini(models: _Models) -> GeminiClient:
    return GeminiClient(
        api_key="offline-placeholder",
        model="gemini-test",
        client_factory=lambda api_key: SimpleNamespace(models=models),
        max_retries=2,
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr("src.llm.gemini_client.sleep", recorded.append)
    return recorded


# --- Gemini (bounded workflow): backoff and classification -------------------------------------


def test_gemini_backs_off_exponentially_and_reports_its_retries(sleeps: list[float]) -> None:
    models = _Models([_api_error(429, "quota", "RESOURCE_EXHAUSTED")] * 2)
    records: list[LLMCallRecord] = []

    ObservedLLMClient(_gemini(models), records.append).generate("q")

    assert models.calls == 3 and sleeps == BACKOFF
    [record] = records
    assert (record.outcome, record.retries, record.prompt_tokens) == ("success", 2, 40)


def test_gemini_gives_up_after_its_retry_budget(sleeps: list[float]) -> None:
    models = _Models([_api_error(503, "overloaded", "UNAVAILABLE")] * 5)

    with pytest.raises(LLMGenerationError):
        _gemini(models).generate("q")

    assert models.calls == 3 and sleeps == BACKOFF  # never a fourth attempt


@pytest.mark.parametrize(
    "error",
    [
        # D1: provider free text holding a status-like number must not make a 400 retryable.
        _api_error(400, "The input token count (1500) exceeds the limit", "INVALID_ARGUMENT"),
        _api_error(400, "Request timed out waiting for 4290 tokens", "INVALID_ARGUMENT"),
        _api_error(403, "rate limit policy denies this key", "PERMISSION_DENIED"),
        RuntimeError("schema rejected: 15003 characters"),
    ],
    ids=["400-token-count", "400-timeout-text", "403-rate-text", "plain-number"],
)
def test_non_transient_errors_are_never_retried(sleeps: list[float], error: Exception) -> None:
    models = _Models([error])

    with pytest.raises(LLMGenerationError):
        _gemini(models).generate("q")

    assert models.calls == 1 and sleeps == []


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (_api_error(429, "x", "RESOURCE_EXHAUSTED"), True),
        (_api_error(500, "x", "INTERNAL"), True),
        (_api_error(503, "x", "UNAVAILABLE"), True),
        (_api_error(504, "x", "DEADLINE_EXCEEDED"), True),
        (_api_error(400, "code 500 mentioned", "INVALID_ARGUMENT"), False),
        (RuntimeError("503 temporarily unavailable"), True),
        (RuntimeError("Read timed out"), True),
        (RuntimeError("HTTP 429"), True),
        (RuntimeError("value 4291 out of range"), False),
        (RuntimeError("connection reset by peer"), True),
    ],
)
def test_retry_classification_uses_the_status_code_first(error: Exception, retryable: bool) -> None:
    assert _is_retryable_error(error) is retryable


# --- HTTP provider clients: backoff, Retry-After, fallback -------------------------------------


class _Wire:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": "answer"}],
            "usage": {"input_tokens": 12, "output_tokens": 3},
        },
    )


def _limited() -> httpx.Response:
    return httpx.Response(429, json={}, headers={"Retry-After": "30"})


def _anthropic(wire: _Wire, sleeps: list[float]) -> AnthropicClient:
    return AnthropicClient(
        "offline-placeholder",
        ModelTiers("claude-strong"),
        {},
        max_retries=2,
        transport=httpx.MockTransport(wire),
        sleeper=sleeps.append,
    )


def test_http_clients_use_the_fixed_backoff_and_ignore_retry_after() -> None:
    sleeps: list[float] = []
    wire = _Wire(_limited(), _limited(), _ok())

    _anthropic(wire, sleeps).generate("q")

    assert wire.requests == 3
    assert sleeps == BACKOFF  # not 30 s: Retry-After is not read (documented limitation)


def test_fallback_follows_exhausted_retries_and_meters_only_what_providers_report() -> None:
    sleeps: list[float] = []
    limited = _Wire(_limited())

    class Answering:
        configured = True
        provider = "scripted"
        model = "scripted-1"

        def generate(self, prompt: str) -> LLMResponse:
            report_usage(prompt_tokens=5, output_tokens=2)
            return LLMResponse(text="ok", model=self.model, provider=self.provider)

    meter = _Meter()
    with meter_provider_calls(meter):
        response = FallbackLLMClient([_anthropic(limited, sleeps), Answering()]).generate("q")

    assert response.provider == "scripted"
    assert limited.requests == 3 and sleeps == BACKOFF
    assert meter.fallbacks == 1
    assert meter.usage == [(5, 2, 0)]  # the rate-limited provider reported nothing billable


# --- concurrency: meters, per-question usage, the API limiter, the ledger ----------------------


@dataclass
class _Meter:
    usage: list[tuple[int | None, int | None, int]] = field(default_factory=list)
    fallbacks: int = 0

    def record_usage(self, prompt: int | None, output: int | None, retries: int) -> None:
        self.usage.append((prompt, output, retries))

    def record_fallbacks(self, count: int) -> None:
        self.fallbacks += count


def test_provider_meters_stay_isolated_across_interleaved_threads() -> None:
    workers = 8
    barrier = threading.Barrier(workers)
    meters = [_Meter() for _ in range(workers)]

    def work(index: int) -> None:
        with meter_provider_calls(meters[index]):
            for step in range(5):
                barrier.wait()  # every thread reports in lockstep
                report_usage(prompt_tokens=index, output_tokens=step)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, range(workers)))

    for index, meter in enumerate(meters):
        assert meter.usage == [(index, step, 0) for step in range(5)]


def test_per_question_usage_stays_isolated_across_concurrent_questions() -> None:
    workers = 6
    barrier = threading.Barrier(workers)

    class Text:
        provider = "scripted"
        model = "scripted-1"
        configured = True

        def __init__(self, tokens: int) -> None:
            self.tokens = tokens

        def generate(self, prompt: str) -> LLMResponse:
            barrier.wait()
            report_usage(prompt_tokens=self.tokens, output_tokens=1)
            return LLMResponse(text="ok", model=self.model, provider=self.provider)

    def question(index: int) -> tuple[int, int | None]:
        client = ObservedLLMClient(Text(tokens=100 + index))
        with collect_question_usage() as usage:
            for _ in range(3):
                client.generate("q")
        return usage.calls, usage.prompt_tokens

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(question, range(workers)))

    assert results == [(3, 3 * (100 + index)) for index in range(workers)]


def test_the_api_rate_limiter_never_admits_more_than_its_limit_under_contention() -> None:
    limiter = InMemoryRateLimiter(clock=lambda: 100.0)
    barrier = threading.Barrier(16)

    def burst(_: int) -> int:
        barrier.wait()
        return sum(
            limiter.consume(
                operation="message",
                tenant_id="t",
                workspace_id="w",
                limit=50,
                window_seconds=60,
            ).allowed
            for _ in range(10)
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        admitted = sum(pool.map(burst, range(16)))

    assert admitted == 50
    other = limiter.consume(
        operation="message", tenant_id="t", workspace_id="other", limit=50, window_seconds=60
    )
    assert other.allowed  # counters are per tenant, workspace, and operation


def test_the_ledger_never_over_reserves_with_latency_between_authorize_and_settle() -> None:
    ledger = BudgetLedger(EXECUTION_LIMITS)
    limit = EXECUTION_LIMITS.model_calls
    per_call = BudgetRequest.model_call(input_tokens=100, output_tokens=50)
    in_flight = 0
    peak = 0
    guard = threading.Lock()

    def call(index: int) -> bool:
        nonlocal in_flight, peak
        try:
            reservation = ledger.authorize(per_call)
        except (BudgetExhaustedError, BudgetClosedError):
            return False
        with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.001 * (index % 3))  # scripted provider latency
        with guard:
            in_flight -= 1
        ledger.settle(reservation, input_tokens=80, output_tokens=40)
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        granted = sum(pool.map(call, range(limit * 3)))

    usage = ledger.usage()
    assert granted == limit
    assert usage.model_calls == limit
    assert usage.input_tokens == 80 * limit and usage.output_tokens == 40 * limit
    assert peak <= 8
