"""Second hosted provider, local model, fallback chain, tier routing, and consent disclosure."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from config.settings import Settings
from packages.evaluation.fakes import RecordingLLM, ScriptedLLM
from packages.evaluation.fixtures import load_fixtures
from packages.evaluation.runner import run_suite
from src.llm.base import LLMConfigurationError, LLMGenerationError, LLMResponse
from src.llm.factory import build_llm_client
from src.llm.fallback import FallbackLLMClient, parse_json_object
from src.llm.gemini_client import GeminiClient, build_gemini_client
from src.llm.observability import (
    LLMCallRecord,
    ObservedLLMClient,
    llm_purpose,
    report_served_by,
)
from src.llm.providers import AnthropicClient, ModelTiers, OllamaClient, select_model
from src.llm.structured import RouteDecision
from tests.test_api_features import FakeLLM, _settings
from tests.test_api_governance import _build, _prepare_tabular_context

SECRET_KEY = "sk-ant-test-secret-key"
PROVIDER_ERROR_BODY = "internal detail: SELECT * FROM payroll"
TIERS = {"route": "fast", "sql_generation": "strong", "rag_answer": "strong"}


def _anthropic_body(text: str = "answer", **usage: int) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": usage.get("i", 11), "output_tokens": usage.get("o", 7)},
    }


class Wire:
    """Scripted HTTP transport that records every request."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def _anthropic(wire: Wire, key: str | None = SECRET_KEY, retries: int = 2) -> AnthropicClient:
    return AnthropicClient(
        key,
        ModelTiers("claude-strong", "claude-fast"),
        TIERS,
        max_retries=retries,
        transport=wire.transport,
        sleeper=lambda _s: None,
    )


# --- Anthropic ---------------------------------------------------------------------------------


def test_anthropic_request_is_fixed_endpoint_untrusted_data_instruction_and_deterministic() -> None:
    wire = Wire(_json(200, _anthropic_body("hello")))

    response = _anthropic(wire).generate("What changed?")

    [request] = wire.requests
    body = wire.bodies()[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == SECRET_KEY
    assert request.headers["anthropic-version"]
    assert body["temperature"] == 0 and body["max_tokens"] > 0
    assert "untrusted" in body["system"]
    assert body["messages"] == [{"role": "user", "content": "What changed?"}]
    assert (response.text, response.provider, response.model) == (
        "hello",
        "anthropic",
        "claude-strong",
    )
    assert SECRET_KEY not in json.dumps(body)


def test_purpose_selects_the_model_tier() -> None:
    wire = Wire(_json(200, _anthropic_body()))
    client = _anthropic(wire)

    with llm_purpose("route"):
        client.generate("classify")
    with llm_purpose("sql_generation"):
        client.generate("write sql")
    client.generate("no declared purpose")

    assert [body["model"] for body in wire.bodies()] == [
        "claude-fast",
        "claude-strong",
        "claude-strong",
    ]


def test_select_model_falls_back_to_strong_without_a_fast_model() -> None:
    with llm_purpose("route"):
        assert select_model(ModelTiers("big"), TIERS) == "big"
        assert select_model(ModelTiers("big", "small"), TIERS) == "small"
        assert select_model(ModelTiers("big", "small"), {}) == "big"


def test_anthropic_reports_usage_retries_and_the_serving_model() -> None:
    wire = Wire(_json(429, {}), _json(200, _anthropic_body(i=30, o=4)))
    records: list[LLMCallRecord] = []
    client = ObservedLLMClient(_anthropic(wire), records.append)

    with llm_purpose("route"):
        client.generate("x")

    [record] = records
    assert (record.provider, record.model) == ("anthropic", "claude-fast")
    assert (record.prompt_tokens, record.output_tokens, record.retries) == (30, 4, 1)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses_are_bounded_then_fail_with_a_generic_message(status: int) -> None:
    wire = Wire(httpx.Response(status, text=PROVIDER_ERROR_BODY))

    with pytest.raises(LLMGenerationError) as captured:
        _anthropic(wire, retries=2).generate("x")

    assert len(wire.requests) == 3  # one attempt plus exactly two retries
    assert PROVIDER_ERROR_BODY not in str(captured.value) and SECRET_KEY not in str(captured.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
def test_other_statuses_fail_immediately_without_retry_or_redirect(status: int) -> None:
    wire = Wire(httpx.Response(status, text=PROVIDER_ERROR_BODY, headers={"location": "http://x"}))

    with pytest.raises(LLMGenerationError) as captured:
        _anthropic(wire).generate("x")

    assert len(wire.requests) == 1  # no retry, and the redirect target was never contacted
    assert PROVIDER_ERROR_BODY not in str(captured.value)


def test_network_errors_are_retried_then_surface_as_a_provider_failure() -> None:
    wire = Wire(
        httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow"), _json(200, _anthropic_body())
    )

    assert _anthropic(wire).generate("x").text == "answer"
    assert len(wire.requests) == 3


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["list"]),
        httpx.Response(200, json={"content": []}),
        httpx.Response(200, json={"content": [{"type": "tool_use"}]}),
        httpx.Response(200, content=b"x" * 2_100_000),
    ],
    ids=["not-json", "array", "no-content", "no-text", "oversized"],
)
def test_malformed_or_oversized_responses_are_provider_failures(response: httpx.Response) -> None:
    with pytest.raises(LLMGenerationError):
        _anthropic(Wire(response)).generate("x")


def test_anthropic_requires_a_key_and_a_prompt_without_touching_the_network() -> None:
    wire = Wire(_json(200, _anthropic_body()))

    with pytest.raises(LLMConfigurationError):
        _anthropic(wire, key=None).generate("x")
    with pytest.raises(ValueError):
        _anthropic(wire).generate("   ")
    assert wire.requests == [] and _anthropic(wire, key=None).configured is False


# --- Ollama ------------------------------------------------------------------------------------


def test_ollama_posts_to_the_configured_endpoint_with_tiered_model_and_reports_usage() -> None:
    wire = Wire(
        _json(
            200, {"message": {"content": "local answer"}, "prompt_eval_count": 9, "eval_count": 3}
        )
    )
    records: list[LLMCallRecord] = []
    client = ObservedLLMClient(
        OllamaClient(
            "http://127.0.0.1:11434/",
            ModelTiers("llama-strong", "llama-fast"),
            TIERS,
            transport=wire.transport,
            sleeper=lambda _s: None,
        ),
        records.append,
    )

    with llm_purpose("route"):
        response = client.generate("hi")

    assert str(wire.requests[0].url) == "http://127.0.0.1:11434/api/chat"
    body = wire.bodies()[0]
    assert body["model"] == "llama-fast" and body["stream"] is False
    assert body["options"] == {"temperature": 0}
    assert response.text == "local answer"
    assert (records[0].prompt_tokens, records[0].output_tokens) == (9, 3)
    assert (records[0].provider, records[0].model) == ("ollama", "llama-fast")


def test_unconfigured_ollama_never_calls_out() -> None:
    wire = Wire(_json(200, {}))
    client = OllamaClient("http://127.0.0.1:11434", None, transport=wire.transport)

    with pytest.raises(LLMConfigurationError):
        client.generate("x")
    assert wire.requests == [] and client.configured is False


# --- fallback chain ----------------------------------------------------------------------------


class Member:
    def __init__(self, name: str, *, fail: bool = False, configured: bool = True) -> None:
        self.provider = name
        self.model = f"{name}-model"
        self.configured = configured
        self.fail = fail
        self.calls = 0

    def generate(self, prompt: str) -> LLMResponse:
        self.calls += 1
        if self.fail:
            raise LLMGenerationError(f"{self.provider} down: {PROVIDER_ERROR_BODY}")
        report_served_by(self.provider, self.model)
        return LLMResponse(f"from {self.provider}", self.model, self.provider)


def test_chain_tries_members_in_order_once_each_and_reports_the_fallback() -> None:
    first, second, third = Member("a", fail=True), Member("b"), Member("c")
    records: list[LLMCallRecord] = []
    client = ObservedLLMClient(FallbackLLMClient([first, second, third]), records.append)

    response = client.generate("x")

    assert response.text == "from b"
    assert (first.calls, second.calls, third.calls) == (1, 1, 0)
    [record] = records
    assert (record.provider, record.model, record.fallbacks) == ("b", "b-model", 1)


def test_chain_does_not_fall_back_when_the_primary_answers() -> None:
    first, second = Member("a"), Member("b")
    records: list[LLMCallRecord] = []

    ObservedLLMClient(FallbackLLMClient([first, second]), records.append).generate("x")

    assert second.calls == 0 and records[0].fallbacks == 0 and records[0].provider == "a"


def test_chain_skips_unconfigured_members_so_withheld_credentials_are_never_used() -> None:
    withheld, usable = Member("hosted", configured=False), Member("local")

    chain = FallbackLLMClient([withheld, usable])

    assert chain.generate("x").text == "from local"
    assert withheld.calls == 0 and chain.provider == "local"


def test_all_members_failing_is_a_generic_provider_failure() -> None:
    chain = FallbackLLMClient([Member("a", fail=True), Member("b", fail=True)])

    with pytest.raises(LLMGenerationError) as captured:
        chain.generate("x")

    assert str(captured.value) == "All configured model providers failed."
    assert PROVIDER_ERROR_BODY not in str(captured.value)


def test_no_usable_member_is_a_configuration_error_and_calls_nothing() -> None:
    members = [Member("a", configured=False), Member("b", configured=False)]

    chain = FallbackLLMClient(members)

    assert chain.configured is False
    with pytest.raises(LLMConfigurationError):
        chain.generate("x")
    assert [m.calls for m in members] == [0, 0]


def test_non_provider_errors_do_not_trigger_fallback() -> None:
    class Bad(Member):
        def generate(self, prompt: str) -> LLMResponse:
            self.calls += 1
            raise ValueError("Prompt cannot be empty.")

    first, second = Bad("a"), Member("b")

    with pytest.raises(ValueError):
        FallbackLLMClient([first, second]).generate("x")
    assert second.calls == 0


def test_a_chain_needs_at_least_two_members() -> None:
    with pytest.raises(ValueError):
        FallbackLLMClient([Member("a")])


def test_structured_calls_use_native_structured_output_or_the_validated_text_path() -> None:
    class Structured(Member):
        def generate_structured(self, prompt: str, schema: Any) -> Any:
            self.calls += 1
            raise LLMGenerationError("native structured failed")

    class TextOnly(Member):
        def generate(self, prompt: str) -> LLMResponse:
            self.calls += 1
            return LLMResponse('```json\n{"route":"sql","confidence":0.9}\n```', "m", "t")

    structured, text_only = Structured("a"), TextOnly("b")

    decision = FallbackLLMClient([structured, text_only]).generate_structured("p", RouteDecision)

    assert decision.route == "sql" and (structured.calls, text_only.calls) == (1, 1)


def test_invalid_structured_text_is_a_validation_error_not_a_provider_failure() -> None:
    class Garbage(Member):
        def generate(self, prompt: str) -> LLMResponse:
            self.calls += 1
            return LLMResponse('{"route":"nonsense","confidence":5}', "m", "t")

    first, second = Garbage("a"), Member("b")

    with pytest.raises(ValueError, match="structured_validation"):
        FallbackLLMClient([first, second]).generate_structured("p", RouteDecision)
    assert second.calls == 0  # a bad answer is the repair loop's concern, not a failover trigger


def test_json_object_extraction_accepts_fences_and_rejects_non_objects() -> None:
    assert parse_json_object('text ```json {"a": 1} ``` more') == {"a": 1}
    for bad in ("no json", "[1, 2]", '{"a": '):
        with pytest.raises(ValueError):
            parse_json_object(bad)


# --- Gemini tiers ------------------------------------------------------------------------------


def test_gemini_uses_the_fast_model_for_fast_purposes_only() -> None:
    calls: list[str] = []

    class Models:
        def generate_content(self, model: str, contents: str, **_: Any) -> Any:
            calls.append(model)
            return SimpleNamespace(text="ok")

    client = GeminiClient(
        "key",
        "gemini-strong",
        client_factory=lambda api_key: SimpleNamespace(models=Models()),
        tiers=ModelTiers("gemini-strong", "gemini-fast"),
        purpose_tiers=TIERS,
    )

    with llm_purpose("route"):
        client.generate("a")
    client.generate("b")

    assert calls == ["gemini-fast", "gemini-strong"]


# --- factory, local-only, and locality ---------------------------------------------------------


def _settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    return replace(_settings(tmp_path), **overrides)


def test_default_configuration_builds_exactly_the_previous_single_gemini_client(
    tmp_path: Path,
) -> None:
    client = build_llm_client(_settings_for(tmp_path))

    assert isinstance(client, GeminiClient) and client.model == "fake-model"


def test_two_providers_build_an_ordered_fallback_chain(tmp_path: Path) -> None:
    settings = _settings_for(
        tmp_path,
        llm_providers=("anthropic", "gemini"),
        anthropic_api_key=SECRET_KEY,
    )

    client = build_llm_client(settings)

    assert isinstance(client, FallbackLLMClient) and client.provider == "anthropic"


def test_local_only_mode_withholds_every_hosted_key_at_construction(tmp_path: Path) -> None:
    settings = _settings_for(
        tmp_path,
        llm_providers=("gemini", "anthropic"),
        anthropic_api_key=SECRET_KEY,
        local_only_mode=True,
    )

    chain: Any = build_llm_client(settings)
    gemini = build_gemini_client(settings)

    assert chain.configured is False and gemini.configured is False
    assert settings.hosted_recipients() == () and settings.hosted_model_configured is False
    with pytest.raises(LLMConfigurationError):
        chain.generate("x")


def test_local_only_mode_still_allows_a_loopback_model(tmp_path: Path) -> None:
    settings = _settings_for(
        tmp_path,
        llm_providers=("ollama",),
        ollama_model="llama3",
        local_only_mode=True,
    )

    client: Any = build_llm_client(settings)

    assert client.configured is True and client.is_local is True
    assert settings.local_model_configured and settings.model_backed_available
    assert settings.hosted_recipients() == ()  # nothing leaves the machine: no consent needed


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://127.0.0.1:11434", True),
        ("http://localhost:11434", True),
        ("http://[::1]:11434", True),
        ("http://127.0.0.5:11434", True),
        ("http://10.0.0.5:11434", False),
        ("http://ollama.example.com", False),
        ("http://localhost.evil.example:11434", False),
        ("http://127.evil.example:11434", False),
        ("https://api.example.com", False),
    ],
)
def test_ollama_counts_as_local_only_for_loopback_endpoints(
    tmp_path: Path, url: str, local: bool
) -> None:
    settings = _settings_for(
        tmp_path, llm_providers=("ollama",), ollama_model="m", ollama_base_url=url
    )

    assert settings.ollama_is_local is local
    assert bool(settings.hosted_recipients()) is (not local)


def test_container_internal_hosts_are_local_only_when_the_operator_declares_them(
    tmp_path: Path,
) -> None:
    undeclared = _settings_for(
        tmp_path, llm_providers=("ollama",), ollama_model="m", ollama_base_url="http://ollama:11434"
    )
    declared = replace(undeclared, ollama_trusted_hosts=("ollama",))

    assert not undeclared.ollama_is_local and declared.ollama_is_local


def test_remote_ollama_is_a_hosted_recipient_and_is_withheld_in_local_only_mode(
    tmp_path: Path,
) -> None:
    remote = _settings_for(
        tmp_path,
        llm_providers=("ollama",),
        ollama_model="m",
        ollama_base_url="http://gpu-box.example.com:11434",
    )
    locked = replace(remote, local_only_mode=True)

    assert remote.hosted_recipients() == ("Ollama (remote)",)
    locked_client: Any = build_llm_client(locked)
    assert locked.hosted_recipients() == () and locked_client.configured is False


def test_recipients_follow_chain_order_and_exclude_unconfigured_providers(tmp_path: Path) -> None:
    settings = _settings_for(
        tmp_path,
        llm_providers=("anthropic", "gemini", "ollama"),
        anthropic_api_key=SECRET_KEY,
        gemini_api_key=None,
        ollama_model="m",
    )

    assert settings.hosted_recipients() == ("Anthropic",)


# --- settings validation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"llm_providers": ()}, "at least one"),
        ({"llm_providers": ("gemini", "openai")}, "unknown"),
        ({"llm_providers": ("gemini", "gemini")}, "repeat"),
        ({"llm_providers": ("ollama",)}, "OLLAMA_MODEL"),
        (
            {"llm_providers": ("ollama",), "ollama_model": "m", "ollama_base_url": "ftp://x"},
            "OLLAMA_BASE_URL",
        ),
        (
            {
                "llm_providers": ("ollama",),
                "ollama_model": "m",
                "ollama_base_url": "http://user:pw@127.0.0.1:11434",
            },
            "OLLAMA_BASE_URL",
        ),
        ({"ollama_trusted_hosts": ("bad host!",)}, "OLLAMA_TRUSTED_HOSTS"),
        ({"llm_purpose_tiers": (("route", "turbo"),)}, "LLM_TIER"),
        ({"llm_purpose_tiers": (("route", "fast"),)}, "LLM_TIER"),
    ],
)
def test_invalid_provider_configuration_fails_closed(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _settings_for(tmp_path, **overrides).validate_llm_configuration()


def test_secrets_are_not_in_the_settings_repr(tmp_path: Path) -> None:
    assert SECRET_KEY not in repr(_settings_for(tmp_path, anthropic_api_key=SECRET_KEY))


# --- consent disclosure ------------------------------------------------------------------------


def _ready_conversation(client: Any) -> tuple[str, str]:
    workspace_id = client.post("/api/v1/workspaces").json()["id"]
    dataset_id, _ = _prepare_tabular_context(client, workspace_id)
    conversation = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"dataset_id": dataset_id}
    ).json()
    return str(conversation["id"]), str(workspace_id)


def _ask(client: Any, conversation_id: str) -> Any:
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages", json={"question": "Top merchants?"}
    )


def test_workspace_and_consent_name_the_providers_that_receive_data(tmp_path: Path) -> None:
    settings = _settings_for(
        tmp_path, llm_providers=("gemini", "anthropic"), anthropic_api_key=SECRET_KEY
    )
    _app, client, _audit, _telemetry = _build(tmp_path, settings=settings)

    workspace = client.post("/api/v1/workspaces").json()
    forged = client.put(
        f"/api/v1/workspaces/{workspace['id']}/consent",
        json={"accepted": True, "notice_version": "2026-09", "data_recipients": ["Nobody"]},
    )
    consent = client.put(
        f"/api/v1/workspaces/{workspace['id']}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    ).json()

    assert workspace["data_recipients"] == ["Gemini", "Anthropic"]
    assert workspace["consent_required"] is True
    assert forged.status_code == 422  # recipients are never accepted from the request
    assert consent["data_recipients"] == ["Gemini", "Anthropic"]  # from server configuration


def test_adding_a_provider_invalidates_prior_consent(tmp_path: Path) -> None:
    app, client, _audit, _telemetry = _build(tmp_path)
    conversation_id, workspace_id = _ready_conversation(client)
    client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )
    assert _ask(client, conversation_id).status_code == 201

    app.state.settings = replace(
        app.state.settings, llm_providers=("gemini", "anthropic"), anthropic_api_key=SECRET_KEY
    )
    blocked = _ask(client, conversation_id)

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "gemini_consent_required"
    client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )
    assert _ask(client, conversation_id).status_code == 201


def test_local_model_needs_no_consent(tmp_path: Path) -> None:
    settings = _settings_for(
        tmp_path,
        gemini_api_key=None,
        llm_providers=("ollama",),
        ollama_model="llama3",
    )
    _app, client, _audit, _telemetry = _build(tmp_path, settings=settings)

    workspace = client.post("/api/v1/workspaces").json()
    conversation_id, _ = _ready_conversation(client)

    assert workspace["consent_required"] is False and workspace["data_recipients"] == []
    assert _ask(client, conversation_id).status_code == 201


# --- fallback is visible in audit and telemetry ------------------------------------------------


def test_fallback_is_recorded_in_audit_and_llm_telemetry(tmp_path: Path) -> None:
    class Primary(FakeLLM):
        provider = "primary"
        model = "primary-model"

        def generate(self, prompt: str) -> LLMResponse:
            raise LLMGenerationError("primary down")

    class Secondary(FakeLLM):
        provider = "secondary"
        model = "secondary-model"

        def generate(self, prompt: str) -> LLMResponse:
            response = super().generate(prompt)
            report_served_by(self.provider, self.model)
            return response

    chain: Any = FallbackLLMClient([Primary(), Secondary()])
    settings = _settings(tmp_path)
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from packages.governance import InMemoryAuditSink
    from packages.observability import InMemoryTelemetrySink
    from tests.test_api_governance import FakeEmbedder

    audit, telemetry = InMemoryAuditSink(), InMemoryTelemetrySink()
    app = create_app(
        settings=settings,
        embedder_factory=lambda _m: FakeEmbedder(),
        llm_client_factory=lambda _s: chain,
        telemetry_sink=telemetry,
        audit_sink=audit,
    )
    client = TestClient(app)
    conversation_id, workspace_id = _ready_conversation(client)
    client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )

    assert _ask(client, conversation_id).status_code == 201

    routed = [e for e in audit.events if e.name == "agent.route_executed"]
    assert routed and all(e.attributes["fallback_used"] is True for e in routed)
    assert all(e.attributes["llm_provider"] == "secondary" for e in routed)
    assert all(e.attributes["llm_model"] == "secondary-model" for e in routed)
    calls = telemetry.named("llm.call")
    assert calls and all(c.attributes["llm_fallbacks"] == 1 for c in calls)
    assert all(c.attributes["llm_provider"] == "secondary" for c in calls)
    answer = telemetry.named("agent.answer")[-1]
    assert answer.attributes["llm_fallbacks"] == answer.attributes["llm_calls"]


# --- evaluation: provider-parameterized safety gates ------------------------------------------


class _Wrapped(RecordingLLM):
    """Runs a scripted fake through a provider wrapper while keeping the harness's recorders."""

    def __init__(self, inner: ScriptedLLM, wrap: Any) -> None:
        super().__init__()
        self.client = wrap(inner)
        self.calls = inner.calls  # shared lists: the inner fake records every prompt it receives
        self.prompts = inner.prompts
        self.configured = inner.configured  # an unscripted classification means "no model"
        self.provider = "wrapped"
        self.model = "wrapped"

    def generate(self, prompt: str) -> LLMResponse:
        return self.client.generate(prompt)


def _observed(inner: Any) -> Any:
    return ObservedLLMClient(inner)


def _chain_with_failing_primary(inner: Any) -> Any:
    class AlwaysConfigured:
        """The scripted fake's own `configured` flag means "no classification model", which the
        harness handles at the top level; as a chain member it must simply be a usable provider."""

        provider = "scripted"
        model = "scripted"
        configured = True

        def generate(self, prompt: str) -> LLMResponse:
            return inner.generate(prompt)

    return ObservedLLMClient(FallbackLLMClient([Member("down", fail=True), AlwaysConfigured()]))


@pytest.mark.parametrize(
    "wrap", [_observed, _chain_with_failing_primary], ids=["observed", "chain"]
)
def test_safety_gates_hold_through_every_provider_wrapper(wrap: Any) -> None:
    fixtures = load_fixtures(Path(__file__).resolve().parents[1] / "evals" / "v1")

    report = run_suite(
        fixtures,
        llm_factory=lambda case: _Wrapped(ScriptedLLM(case.get("script", {})), wrap),
        case_filter=lambda case: "evidence" not in case,
    )

    failed = [c["id"] for c in report["cases"] if not c["passed"]]
    assert failed == [] and report["aggregate"]["critical_failures"] == []
