"""Build the configured model client from server settings (never from request input)."""

from __future__ import annotations

from typing import Any

from config.settings import Settings
from src.llm.base import LLMClient
from src.llm.fallback import FallbackLLMClient
from src.llm.gemini_client import build_gemini_client
from src.llm.providers import AnthropicClient, ModelTiers, OllamaClient


def build_llm_client(settings: Settings) -> LLMClient:
    """One client per configured provider, chained in order when there is more than one.

    Hosted credentials are withheld in local-only mode at construction (as for Gemini), so a hosted
    provider can never send data in that mode. Ollama keeps working there only when its endpoint is
    local (loopback or an operator-declared container-internal host).
    """
    purpose_tiers = dict(settings.llm_purpose_tiers)
    members: list[Any] = []
    for name in settings.llm_providers:
        if name == "gemini":
            members.append(build_gemini_client(settings))
        elif name == "anthropic":
            members.append(
                AnthropicClient(
                    api_key=None if settings.local_only_mode else settings.anthropic_api_key,
                    tiers=ModelTiers(settings.anthropic_model, settings.anthropic_model_fast),
                    purpose_tiers=purpose_tiers,
                )
            )
        elif name == "ollama":
            usable = bool(settings.ollama_model) and (
                settings.ollama_is_local or not settings.local_only_mode
            )
            members.append(
                OllamaClient(
                    base_url=settings.ollama_base_url,
                    tiers=(
                        ModelTiers(str(settings.ollama_model), settings.ollama_model_fast)
                        if usable
                        else None
                    ),
                    purpose_tiers=purpose_tiers,
                    is_local=settings.ollama_is_local,
                )
            )
    client: Any = members[0] if len(members) == 1 else FallbackLLMClient(members)
    return client
