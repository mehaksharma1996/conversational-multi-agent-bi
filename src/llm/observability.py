"""Provider-neutral, content-free observation of model calls.

``ObservedLLMClient`` wraps any ``LLMClient`` and reports exactly one :class:`LLMCallRecord` per
call (text or structured) to an observer and to the per-question usage collector. A record holds
only the call purpose, provider, model, duration, token counts, retry count, outcome, a coarse
error category, and an estimated cost. It never holds a prompt, completion, SQL, row, excerpt, or
file name; the wrapper never reads response text.

Call sites declare *why* they are calling with :func:`llm_purpose`. Providers that know their token
usage report it with :func:`report_usage`; clients that do not simply leave the counts as ``None``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter
from typing import Any

LLM_PURPOSES: frozenset[str] = frozenset(
    {"route", "sql_generation", "sql_correction", "rag_answer", "criteria_extraction"}
)
UNKNOWN_PURPOSE = "unspecified"


@dataclass(frozen=True)
class LLMPricing:
    """Operator-configured USD prices per million tokens; estimates only, never billing."""

    input_per_million_usd: float
    output_per_million_usd: float

    def estimate(self, prompt_tokens: int | None, output_tokens: int | None) -> float | None:
        if prompt_tokens is None and output_tokens is None:
            return None
        return (
            (prompt_tokens or 0) * self.input_per_million_usd
            + (output_tokens or 0) * self.output_per_million_usd
        ) / 1_000_000


@dataclass(frozen=True)
class LLMCallRecord:
    purpose: str
    provider: str
    model: str
    duration_ms: float
    outcome: str  # "success" | "failure"
    retries: int = 0
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    error_category: str | None = None


Observer = Callable[[LLMCallRecord], None]

_PURPOSE: ContextVar[str | None] = ContextVar("llm_purpose", default=None)


@contextmanager
def llm_purpose(purpose: str) -> Iterator[None]:
    """Label model calls made inside the block (one of :data:`LLM_PURPOSES`)."""
    if purpose not in LLM_PURPOSES:
        raise ValueError(f"Unknown LLM purpose: {purpose!r}")
    token = _PURPOSE.set(purpose)
    try:
        yield
    finally:
        _PURPOSE.reset(token)


class _UsageSlot:
    def __init__(self) -> None:
        self.prompt_tokens: int | None = None
        self.output_tokens: int | None = None
        self.retries = 0


_SLOT: ContextVar[_UsageSlot | None] = ContextVar("llm_usage_slot", default=None)


def report_usage(
    *,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    retries: int = 0,
) -> None:
    """Called by a provider client while a call is in flight; a no-op when nothing observes it."""
    slot = _SLOT.get()
    if slot is None:
        return
    slot.prompt_tokens = _count(prompt_tokens)
    slot.output_tokens = _count(output_tokens)
    slot.retries = max(0, int(retries))


class QuestionUsage:
    """Thread-safe totals for the model calls made while answering one question."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.failures = 0
        self.prompt_tokens: int | None = None
        self.output_tokens: int | None = None
        self.duration_ms = 0.0
        self.estimated_cost_usd: float | None = None

    def add(self, record: LLMCallRecord) -> None:
        with self._lock:
            self.calls += 1
            self.failures += int(record.outcome != "success")
            self.duration_ms += record.duration_ms
            self.prompt_tokens = _sum(self.prompt_tokens, record.prompt_tokens)
            self.output_tokens = _sum(self.output_tokens, record.output_tokens)
            if record.estimated_cost_usd is not None:
                self.estimated_cost_usd = (self.estimated_cost_usd or 0.0) + (
                    record.estimated_cost_usd
                )


_COLLECTOR: ContextVar[QuestionUsage | None] = ContextVar("llm_question_usage", default=None)


@contextmanager
def collect_question_usage() -> Iterator[QuestionUsage]:
    usage = QuestionUsage()
    token = _COLLECTOR.set(usage)
    try:
        yield usage
    finally:
        _COLLECTOR.reset(token)


class ObservedLLMClient:
    """Decorator that observes every call of the wrapped client without touching its content."""

    def __init__(
        self,
        inner: Any,
        observer: Observer | None = None,
        pricing: LLMPricing | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._inner = inner
        self.provider: str = str(getattr(inner, "provider", "unknown"))
        self.model: str = str(getattr(inner, "model", "unknown"))
        self._observer = observer
        self._pricing = pricing
        self._clock = clock

    def __getattr__(self, name: str) -> Any:
        # Forward capability flags such as ``configured``. Dunder lookups never reach here.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def generate(self, prompt: str) -> Any:
        return self._observe(lambda: self._inner.generate(prompt))

    @property
    def generate_structured(self) -> Callable[..., Any]:
        inner = getattr(self._inner, "generate_structured", None)
        if not callable(inner):
            # Keep capability detection (`getattr(client, "generate_structured", None)`) truthful.
            raise AttributeError("generate_structured")
        return lambda prompt, schema_model: self._observe(lambda: inner(prompt, schema_model))

    def _observe(self, call: Callable[[], Any]) -> Any:
        slot = _UsageSlot()
        token = _SLOT.set(slot)
        started = self._clock()
        outcome = "success"
        category: str | None = None
        try:
            return call()
        except BaseException as exc:
            outcome = "failure"
            category = _error_category(exc)
            raise
        finally:
            _SLOT.reset(token)
            record = LLMCallRecord(
                purpose=_PURPOSE.get() or UNKNOWN_PURPOSE,
                provider=self.provider,
                model=self.model,
                duration_ms=(self._clock() - started) * 1000,
                outcome=outcome,
                retries=slot.retries,
                prompt_tokens=slot.prompt_tokens,
                output_tokens=slot.output_tokens,
                estimated_cost_usd=(
                    self._pricing.estimate(slot.prompt_tokens, slot.output_tokens)
                    if self._pricing is not None
                    else None
                ),
                error_category=category,
            )
            self._deliver(record)

    def _deliver(self, record: LLMCallRecord) -> None:
        collector = _COLLECTOR.get()
        if collector is not None:
            collector.add(record)
        if self._observer is not None:
            try:
                self._observer(record)
            except Exception:  # observation must never change the outcome of a model call
                return


def _error_category(exc: BaseException) -> str:
    names = {cls.__name__ for cls in type(exc).__mro__}
    if "LLMConfigurationError" in names:
        return "provider_configuration"
    if "LLMGenerationError" in names:
        return "provider_failure"
    if "TimeoutError" in names:
        return "timeout"
    if "ValueError" in names or "ValidationError" in names:
        return "invalid_input"
    return "internal"


def _count(value: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _sum(total: int | None, addition: int | None) -> int | None:
    if addition is None:
        return total
    return (total or 0) + addition
