"""LLM call observation: one content-free event per call, aggregation, pricing, and privacy."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from config.settings import Settings
from packages.evaluation.runner import token_usage
from packages.observability import InMemoryTelemetrySink, Telemetry
from packages.observability.telemetry import ALLOWED_ATTRIBUTES
from src.agents.sql_agent import generate_sql
from src.llm.base import LLMGenerationError, LLMResponse
from src.llm.gemini_client import GeminiClient
from src.llm.observability import (
    LLM_PURPOSES,
    LLMCallRecord,
    LLMPricing,
    ObservedLLMClient,
    collect_question_usage,
    llm_purpose,
    report_usage,
)
from src.llm.structured import HybridCriteria, RouteDecision
from src.orchestration.langgraph_orchestrator import QuestionOrchestrator
from tests.test_api_governance import _build, _run_journey

SECRET_PROMPT = "What is the revenue of Acme Corp? SELECT secret FROM payroll"
SECRET_ANSWER = "Acme revenue is 9,999,999 per policy.pdf page 3"


class TextClient:
    provider = "fake"
    model = "fake-model"
    configured = True

    def __init__(self, text: str = SECRET_ANSWER) -> None:
        self.text = text
        self.calls = 0

    def generate(self, prompt: str) -> LLMResponse:
        self.calls += 1
        report_usage(prompt_tokens=100, output_tokens=40, retries=1)
        return LLMResponse(self.text, self.model, self.provider)


class StructuredClient(TextClient):
    def generate_structured(self, prompt: str, schema_model: Any) -> Any:
        self.calls += 1
        report_usage(prompt_tokens=10, output_tokens=5)
        return schema_model.model_validate({"route": "sql", "confidence": 0.9})


class FailingClient(TextClient):
    def generate(self, prompt: str) -> LLMResponse:
        raise LLMGenerationError(f"provider said: {SECRET_PROMPT}")


def _observed(inner: Any, pricing: LLMPricing | None = None) -> tuple[ObservedLLMClient, list]:
    records: list[LLMCallRecord] = []
    ticks = iter(range(0, 1_000_000, 2))
    return ObservedLLMClient(
        inner, records.append, pricing, clock=lambda: next(ticks) / 1000
    ), records


# --- wrapper -----------------------------------------------------------------------------------


def test_exactly_one_record_per_call_with_purpose_usage_and_duration() -> None:
    client, records = _observed(TextClient())

    with llm_purpose("rag_answer"):
        client.generate(SECRET_PROMPT)
    client.generate(SECRET_PROMPT)

    assert [(r.purpose, r.outcome) for r in records] == [
        ("rag_answer", "success"),
        ("unspecified", "success"),
    ]
    first = records[0]
    assert (first.prompt_tokens, first.output_tokens, first.retries) == (100, 40, 1)
    assert first.provider == "fake" and first.model == "fake-model"
    assert first.duration_ms == pytest.approx(2.0)


def test_structured_calls_are_observed_once_and_capability_stays_truthful() -> None:
    structured, records = _observed(StructuredClient())
    text_only, _ = _observed(TextClient())

    with llm_purpose("route"):
        decision = structured.generate_structured("classify", RouteDecision)

    assert decision.route == "sql"
    assert [(r.purpose, r.prompt_tokens) for r in records] == [("route", 10)]
    assert callable(getattr(structured, "generate_structured", None))
    assert getattr(text_only, "generate_structured", None) is None
    assert structured.configured is True  # capability flags are forwarded


def test_failures_are_recorded_with_a_category_and_still_raised() -> None:
    client, records = _observed(FailingClient())

    with pytest.raises(LLMGenerationError):
        client.generate(SECRET_PROMPT)

    [record] = records
    assert (record.outcome, record.error_category) == ("failure", "provider_failure")
    assert record.prompt_tokens is None
    assert SECRET_PROMPT not in repr(record)


def test_observer_errors_never_change_the_model_result() -> None:
    def broken(_record: LLMCallRecord) -> None:
        raise RuntimeError("exporter down")

    client = ObservedLLMClient(TextClient(), broken)

    assert client.generate("x").text == SECRET_ANSWER


def test_records_never_contain_prompt_or_completion_text() -> None:
    client, records = _observed(TextClient(), LLMPricing(1.0, 2.0))

    with llm_purpose("sql_generation"):
        client.generate(SECRET_PROMPT)

    blob = repr(records)
    for fragment in ("Acme", "SELECT", "payroll", "policy.pdf", "9,999,999"):
        assert fragment not in blob


def test_unknown_purpose_is_a_programming_error() -> None:
    with pytest.raises(ValueError), llm_purpose("free text purpose"):
        pass
    assert "route" in LLM_PURPOSES


def test_cost_is_estimated_only_when_priced_and_counted() -> None:
    priced, priced_records = _observed(TextClient(), LLMPricing(1.0, 4.0))
    unpriced, unpriced_records = _observed(TextClient())
    silent, silent_records = _observed(
        SimpleNamespace(provider="x", model="y", generate=lambda p: p)
    )

    priced.generate("x")
    unpriced.generate("x")
    silent.generate("x")

    assert priced_records[0].estimated_cost_usd == pytest.approx((100 * 1.0 + 40 * 4.0) / 1e6)
    assert unpriced_records[0].estimated_cost_usd is None
    assert silent_records[0].prompt_tokens is None and silent_records[0].estimated_cost_usd is None


def test_question_usage_aggregates_calls_tokens_cost_and_failures() -> None:
    good, _ = _observed(TextClient(), LLMPricing(10.0, 10.0))
    bad, _ = _observed(FailingClient(), LLMPricing(10.0, 10.0))

    with collect_question_usage() as usage:
        good.generate("a")
        good.generate("b")
        with pytest.raises(LLMGenerationError):
            bad.generate("c")

    assert (usage.calls, usage.failures) == (3, 1)
    assert (usage.prompt_tokens, usage.output_tokens) == (200, 80)
    assert usage.estimated_cost_usd == pytest.approx(2800 / 1e6)
    assert usage.duration_ms > 0


def test_agents_and_orchestrator_label_their_purposes() -> None:
    client, records = _observed(TextClient("SELECT 1"))
    structured, structured_records = _observed(StructuredClient())

    generate_sql("p", client)
    generate_sql("p", client, "sql_correction")
    orchestrator = QuestionOrchestrator(llm_client=structured)
    orchestrator._generate_validated("p", RouteDecision)
    try:
        orchestrator._generate_validated("p", HybridCriteria)
    except ValueError:
        pass  # the stub returns a route payload; only the purpose label matters here

    assert [r.purpose for r in records] == ["sql_generation", "sql_correction"]
    assert [r.purpose for r in structured_records][0] == "route"
    assert {r.purpose for r in structured_records[1:]} == {"criteria_extraction"}


# --- Gemini usage ------------------------------------------------------------------------------


class _Models:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures

    def generate_content(self, **kwargs: Any) -> Any:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("503 temporarily unavailable")
        return SimpleNamespace(
            text='{"route":"sql","confidence":0.9}',
            usage_metadata=SimpleNamespace(prompt_token_count=321, candidates_token_count=45),
        )


def _gemini(failures: int = 0) -> GeminiClient:
    models = _Models(failures)
    return GeminiClient(
        api_key="key",
        model="gemini-test",
        client_factory=lambda api_key: SimpleNamespace(models=models),
        max_retries=2,
    )


def test_gemini_reports_token_usage_and_retries_for_text_and_structured_calls(monkeypatch) -> None:
    monkeypatch.setattr("src.llm.gemini_client.sleep", lambda _s: None)
    client, records = _observed(_gemini(failures=1), LLMPricing(2.0, 8.0))

    client.generate("hello")
    client.generate_structured("classify", RouteDecision)

    assert [(r.prompt_tokens, r.output_tokens) for r in records] == [(321, 45)] * 2
    assert [r.retries for r in records] == [1, 0]
    assert records[0].estimated_cost_usd == pytest.approx((321 * 2.0 + 45 * 8.0) / 1e6)


def test_gemini_failures_are_recorded_once_after_internal_retries(monkeypatch) -> None:
    monkeypatch.setattr("src.llm.gemini_client.sleep", lambda _s: None)
    client, records = _observed(_gemini(failures=10))

    with pytest.raises(LLMGenerationError):
        client.generate("hello")

    [record] = records
    assert record.outcome == "failure" and record.error_category == "provider_failure"


# --- telemetry allowlist -----------------------------------------------------------------------


def test_adversarial_values_are_dropped_and_counted_not_exported() -> None:
    sink = InMemoryTelemetrySink()

    Telemetry(sink).emit(
        "llm.call",
        llm_purpose=SECRET_PROMPT,  # free text under an allowlisted name
        llm_model="SELECT * FROM payroll",
        llm_provider="policy pdf",
        llm_prompt_tokens="a lot",
        llm_output_tokens=True,
        llm_estimated_cost_microusd=1.5,
        llm_retries="2",
        question=SECRET_PROMPT,  # unknown names
        sql="SELECT 1",
        filename="policy.pdf",
        completion=SECRET_ANSWER,
        llm_purpose_ok="route",
    )

    [event] = sink.events
    assert event.attributes == {}
    assert event.dropped_attributes == 12
    assert "Acme" not in repr(event) and "policy.pdf" not in repr(event)


def test_every_new_attribute_has_a_declared_safe_shape() -> None:
    expected = {
        "llm_purpose": "token",
        "llm_prompt_tokens": "int",
        "llm_output_tokens": "int",
        "llm_estimated_cost_microusd": "int",
        "llm_retries": "int",
        "llm_calls": "int",
        "llm_failures": "int",
        "llm_duration_ms": "float",
    }
    assert {name: ALLOWED_ATTRIBUTES[name] for name in expected} == expected


# --- API integration ---------------------------------------------------------------------------


class UsageLLM:
    """The API test double, reporting usage like a real provider."""

    def __new__(cls) -> Any:
        from tests.test_api_features import FakeLLM

        class _Usage(FakeLLM):
            def generate(self, prompt: str) -> LLMResponse:
                report_usage(prompt_tokens=50, output_tokens=7)
                return super().generate(prompt)

        return _Usage()


def test_api_emits_one_llm_call_event_per_model_call_with_only_allowlisted_attributes(
    tmp_path: Path,
) -> None:
    settings = replace(
        _settings_with_prices(tmp_path),
        llm_input_cost_per_million_usd=1.0,
        llm_output_cost_per_million_usd=2.0,
    )
    _app, client, _audit, telemetry = _build(tmp_path, UsageLLM(), settings)

    _run_journey(client)

    calls = telemetry.named("llm.call")
    answers = telemetry.named("agent.answer")
    assert calls and answers
    total_calls = sum(int(event.attributes["llm_calls"]) for event in answers)
    assert len(calls) == total_calls
    allowed = {
        "llm_purpose",
        "llm_provider",
        "llm_model",
        "duration_ms",
        "outcome",
        "llm_retries",
        "llm_prompt_tokens",
        "llm_output_tokens",
        "llm_estimated_cost_microusd",
        "error_category",
    }
    for event in calls:
        assert set(event.attributes) <= allowed
        assert event.dropped_attributes == 0
        assert event.attributes["llm_purpose"] in LLM_PURPOSES
        assert event.attributes["llm_prompt_tokens"] == 50
        assert event.attributes["llm_estimated_cost_microusd"] == 64  # 50*1 + 7*2 micro-USD
        assert event.request_id
    purposes = {event.attributes["llm_purpose"] for event in calls}
    assert {"route", "rag_answer"} <= purposes
    for event in answers:
        assert event.attributes["llm_prompt_tokens"] == 50 * event.attributes["llm_calls"]
        assert event.attributes["llm_failures"] == 0
        assert "llm_duration_ms" in event.attributes
    blob = repr(telemetry.events)
    for fragment in ("Which uploaded transactions", "policy.pdf", "SELECT", "Safe Merchant"):
        assert fragment not in blob


def test_api_without_usage_reporting_keeps_token_fields_absent(tmp_path: Path) -> None:
    _app, client, _audit, telemetry = _build(tmp_path)

    _run_journey(client)

    for event in telemetry.named("llm.call"):
        assert "llm_prompt_tokens" not in event.attributes
        assert "llm_estimated_cost_microusd" not in event.attributes
        assert event.dropped_attributes == 0


# --- evaluation report metadata ----------------------------------------------------------------


def test_report_token_usage_is_null_without_usage_and_summed_with_it() -> None:
    from packages.evaluation.models import CaseResult

    def case(diagnostics: dict[str, Any]) -> CaseResult:
        return CaseResult("c", "routing", None, None, "answered", diagnostics=diagnostics)

    scripted = [case({"llm_calls": 0, "llm_prompt_tokens": None, "llm_output_tokens": None})]
    live = [
        case(
            {
                "llm_calls": 2,
                "llm_prompt_tokens": 100,
                "llm_output_tokens": 10,
                "llm_estimated_cost_usd": 0.000123,
            }
        ),
        case(
            {
                "llm_calls": 1,
                "llm_prompt_tokens": 50,
                "llm_output_tokens": 5,
                "llm_estimated_cost_usd": 0.000077,
            }
        ),
        case({}),
    ]

    assert token_usage(scripted) is None
    assert token_usage(live) == {
        "calls": 3,
        "prompt_tokens": 150,
        "output_tokens": 15,
        "estimated_cost_usd": 0.0002,
    }


# --- settings ----------------------------------------------------------------------------------


def _settings_with_prices(root: Path) -> Settings:
    from tests.test_api_features import _settings

    return _settings(root)


@pytest.mark.parametrize(
    "prices",
    [(1.0, None), (None, 1.0), (-1.0, 1.0), (1.0, float("nan")), (1.0, float("inf")), (1.0, 1e9)],
)
def test_partial_or_absurd_pricing_is_rejected(tmp_path: Path, prices: tuple) -> None:
    settings = replace(
        _settings_with_prices(tmp_path),
        llm_input_cost_per_million_usd=prices[0],
        llm_output_cost_per_million_usd=prices[1],
    )

    with pytest.raises(ValueError, match="LLM"):
        settings.validate_llm_pricing()


def test_valid_or_absent_pricing_is_accepted(tmp_path: Path) -> None:
    base = _settings_with_prices(tmp_path)

    base.validate_llm_pricing()
    replace(
        base, llm_input_cost_per_million_usd=0.3, llm_output_cost_per_million_usd=2.5
    ).validate_llm_pricing()
