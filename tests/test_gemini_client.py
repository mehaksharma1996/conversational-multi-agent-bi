"""Tests for the Gemini LLM wrapper."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.settings import Settings
from src.llm.base import LLMConfigurationError, LLMGenerationError
from src.llm.gemini_client import GeminiClient, build_gemini_client
from src.llm.structured import RouteDecision


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
    assert config["response_schema"] is RouteDecision


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
