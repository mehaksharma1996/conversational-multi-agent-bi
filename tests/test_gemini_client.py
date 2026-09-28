"""Tests for the Gemini LLM wrapper."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.llm.base import LLMConfigurationError, LLMGenerationError
from src.llm.gemini_client import GeminiClient


class FakeModels:
    def __init__(self, text: str | None = "hello", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = []

    def generate_content(self, model: str, contents: str):
        self.calls.append({"model": model, "contents": contents})
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
    assert fake_models.calls == [
        {"model": "gemini-2.5-flash", "contents": "What changed?"}
    ]


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
