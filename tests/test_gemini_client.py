"""Tests for the Gemini LLM wrapper."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google import genai
from pydantic import BaseModel

from config.settings import Settings
from src.llm.base import LLMConfigurationError, LLMGenerationError
from src.llm.gemini_client import GeminiClient, build_gemini_client
from src.llm.structured import HybridCriteria, RouteDecision


class FakeModels:
    def __init__(self, text: str | None = "hello", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[dict[str, object]] = []

    def generate_content(self, model: str, contents: str, **kwargs):
        self.calls.append({"model": model, "contents": contents, **kwargs})
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text)


class FakeGenAIClient:
    def __init__(self, models: FakeModels) -> None:
        self.models = models


def test_gemini_client_requires_api_key() -> None:
    client = GeminiClient(api_key=None, model="gemini-2.5-flash")

    with pytest.raises(LLMConfigurationError):
        client.generate("Summarize this data.")


def test_gemini_client_rejects_empty_prompt() -> None:
    client = GeminiClient(api_key="key", model="gemini-2.5-flash")

    with pytest.raises(ValueError):
        client.generate("  ")


def test_gemini_client_generates_text_with_injected_client() -> None:
    fake_models = FakeModels(text="A concise answer.")
    fake_client = FakeGenAIClient(models=fake_models)
    client = GeminiClient(
        api_key="key",
        model="gemini-2.5-flash",
        client_factory=lambda api_key: fake_client,
    )

    response = client.generate("What changed?")

    assert response.text == "A concise answer."
    assert response.model == "gemini-2.5-flash"
    assert response.provider == "gemini"
    assert fake_models.calls == [{"model": "gemini-2.5-flash", "contents": "What changed?"}]


def test_gemini_client_requests_and_validates_structured_json() -> None:
    fake_models = FakeModels(text='{"route":"sql","confidence":0.9}')
    client = GeminiClient(
        api_key="key",
        model="gemini-2.5-flash",
        client_factory=lambda api_key: FakeGenAIClient(models=fake_models),
    )
    client._use_generation_config = True

    result = client.generate_structured("Choose a route.", RouteDecision)

    assert result == RouteDecision(route="sql", confidence=0.9)
    config = fake_models.calls[0]["config"]
    assert isinstance(config, dict)
    assert config["response_mime_type"] == "application/json"
    assert "response_schema" not in config
    assert config["response_json_schema"] == RouteDecision.model_json_schema()


def test_gemini_client_wraps_provider_errors() -> None:
    fake_client = FakeGenAIClient(models=FakeModels(error=RuntimeError("boom")))
    client = GeminiClient(
        api_key="key",
        model="gemini-2.5-flash",
        client_factory=lambda api_key: fake_client,
    )

    with pytest.raises(LLMGenerationError):
        client.generate("Try this.")


def test_gemini_client_rejects_empty_provider_response() -> None:
    fake_client = FakeGenAIClient(models=FakeModels(text=""))
    client = GeminiClient(
        api_key="key",
        model="gemini-2.5-flash",
        client_factory=lambda api_key: fake_client,
    )

    with pytest.raises(LLMGenerationError):
        client.generate("Try this.")


def _settings(**overrides) -> Settings:
    base = Settings(
        app_data_dir=Path("data"),
        sqlite_db_path=Path("data/sqlite/app.db"),
        chroma_persist_dir=Path("data/vectorstore"),
        gemini_api_key="real-key",
        gemini_model="gemini-2.5-flash",
        embedding_model="all-MiniLM-L6-v2",
    )
    return replace(base, **overrides)


def test_build_gemini_client_uses_configured_api_key() -> None:
    client = build_gemini_client(_settings())

    assert client.configured is True


def test_build_gemini_client_withholds_key_in_local_only_mode() -> None:
    client = build_gemini_client(_settings(local_only_mode=True))

    assert client.configured is False


STRUCTURED_MODELS = [RouteDecision, HybridCriteria]


def _walk(node: Any):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _structured_request_body(schema_model: type[BaseModel]) -> dict[str, Any]:
    """Send a structured call through the real SDK and capture the outgoing HTTP body."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "not json"}], "role": "model"}}]},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeminiClient(
        api_key="key",
        model="gemini-2.5-flash",
        client_factory=lambda api_key: genai.Client(
            api_key=api_key, http_options={"httpx_client": http_client}
        ),
    )
    client._use_generation_config = True
    with pytest.raises(ValueError, match="structured_validation"):
        client.generate_structured("Decide.", schema_model)
    return captured["body"]


@pytest.mark.parametrize("schema_model", STRUCTURED_MODELS)
def test_structured_request_keeps_additional_properties_false_for_the_api(
    schema_model: type[BaseModel],
) -> None:
    generation_config = _structured_request_body(schema_model)["generationConfig"]

    assert "responseSchema" not in generation_config
    schema = generation_config["responseJsonSchema"]
    assert generation_config["responseMimeType"] == "application/json"
    assert schema["additionalProperties"] is False
    # The snake_case spelling is what the API rejected with a 400.
    assert "additional_properties" not in json.dumps(generation_config)
    objects = [
        node for node in _walk(schema) if isinstance(node, dict) and node.get("type") == "object"
    ]
    assert objects
    assert all(node["additionalProperties"] is False for node in objects)


def test_hybrid_criteria_request_forbids_extra_fields_on_nested_predicates() -> None:
    schema = _structured_request_body(HybridCriteria)["generationConfig"]["responseJsonSchema"]

    assert schema["$defs"]["PolicyPredicate"]["additionalProperties"] is False
    assert schema["properties"]["predicates"]["anyOf"][0]["items"] == {
        "$ref": "#/$defs/PolicyPredicate"
    }


@pytest.mark.parametrize(
    ("schema_model", "text"),
    [
        (RouteDecision, '{"route":"sql","confidence":0.9,"reason":"extra"}'),
        (RouteDecision, '{"route":"delete_everything","confidence":0.9}'),
        (RouteDecision, '{"route":"sql","confidence":"0.9"}'),
        (RouteDecision, '{"route":"sql","confidence":1.5}'),
        (RouteDecision, '{"route":"sql"}'),
        (RouteDecision, "route: sql"),
        (HybridCriteria, '{"keywords":["a"],"sql":"DROP TABLE x"}'),
        (
            HybridCriteria,
            '{"predicates":[{"field":"amount","operator":"gt","value":5,"source":1,'
            '"raw_sql":"1=1"}]}',
        ),
        (
            HybridCriteria,
            '{"predicates":[{"field":"amount","operator":"between","value":5,"source":1}]}',
        ),
        (HybridCriteria, '{"predicate_join":"none"}'),
        (HybridCriteria, "not json"),
    ],
)
def test_malformed_structured_output_fails_instead_of_becoming_a_decision(
    schema_model: type[BaseModel], text: str
) -> None:
    client = GeminiClient(
        api_key="key",
        model="gemini-2.5-flash",
        client_factory=lambda api_key: FakeGenAIClient(models=FakeModels(text=text)),
    )

    with pytest.raises(ValueError, match="structured_validation"):
        client.generate_structured("Decide.", schema_model)


def test_well_formed_nested_hybrid_criteria_is_accepted() -> None:
    text = (
        '{"predicates":[{"field":"amount","operator":"gt","value":5,"source":1}],'
        '"predicate_join":"all"}'
    )
    client = GeminiClient(
        api_key="key",
        model="gemini-2.5-flash",
        client_factory=lambda api_key: FakeGenAIClient(models=FakeModels(text=text)),
    )

    result = client.generate_structured("Decide.", HybridCriteria)

    assert result.predicates is not None
    assert result.predicates[0].field == "amount"
    assert result.predicate_join == "all"
