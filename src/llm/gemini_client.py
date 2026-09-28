"""Gemini LLM client wrapper."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from config.settings import Settings
from src.llm.base import LLMConfigurationError, LLMGenerationError, LLMResponse

GEMINI_PROVIDER = "gemini"


class GeminiClient:
    """Thin wrapper around the Google GenAI SDK."""

    provider = GEMINI_PROVIDER

    def __init__(
        self,
        api_key: str | None,
        model: str,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._client_factory = client_factory
        self._client: Any | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def generate(self, prompt: str) -> LLMResponse:
        """Generate text using Gemini."""
        if not self.configured:
            raise LLMConfigurationError(
                "Gemini is not configured. Set GEMINI_API_KEY in your .env file."
            )

        if not prompt.strip():
            raise ValueError("Prompt cannot be empty.")

        try:
            response = self._get_client().models.generate_content(
                model=self.model,
                contents=prompt,
            )
        except Exception as exc:
            raise LLMGenerationError(f"Gemini generation failed: {exc}") from exc

        text = getattr(response, "text", None)
        if not text:
            raise LLMGenerationError("Gemini returned an empty response.")

        return LLMResponse(
            text=text,
            model=self.model,
            provider=self.provider,
        )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        if self._client_factory is None:
            from google import genai

            self._client_factory = genai.Client

        self._client = self._client_factory(api_key=self.api_key)
        return self._client


def build_gemini_client(settings: Settings) -> GeminiClient:
    """Create a Gemini client from app settings."""
    return GeminiClient(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
