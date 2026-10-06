"""Ordered fallback across configured model providers.

A member is tried at most once per call, after its own bounded retries. Only
``LLMGenerationError`` (a provider failure) moves the chain on; validation errors and other
exceptions propagate unchanged so the structured-output repair logic and safe error handling behave
exactly as with a single provider. Unconfigured members (no key, withheld in local-only mode) are
skipped, so a configured chain can never send data to a provider whose credentials were withheld.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from src.llm.base import LLMConfigurationError, LLMGenerationError, LLMResponse, SchemaT
from src.llm.observability import report_fallbacks


def parse_json_object(text: str) -> dict:
    """Extract one JSON object from model text (optionally fenced), or raise ``ValueError``."""
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("No JSON object was returned.")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    return payload


class FallbackLLMClient:
    def __init__(self, members: list[Any]) -> None:
        if len(members) < 2:
            raise ValueError("A fallback chain needs at least two providers.")
        self._members = list(members)

    @property
    def _usable(self) -> list[Any]:
        return [member for member in self._members if getattr(member, "configured", False)]

    @property
    def configured(self) -> bool:
        return bool(self._usable)

    @property
    def provider(self) -> str:
        first = (self._usable or self._members)[0]
        return str(first.provider)

    @property
    def model(self) -> str:
        first = (self._usable or self._members)[0]
        return str(first.model)

    def generate(self, prompt: str) -> LLMResponse:
        return self._run(lambda member: member.generate(prompt))

    def generate_structured(self, prompt: str, schema_model: type[SchemaT]) -> SchemaT:
        def call(member: Any) -> SchemaT:
            structured = getattr(member, "generate_structured", None)
            if callable(structured):
                return structured(prompt, schema_model)
            response = member.generate(prompt)
            try:
                return schema_model.model_validate(parse_json_object(response.text))
            except (ValidationError, ValueError, TypeError) as exc:
                raise ValueError("structured_validation") from exc

        return self._run(call)

    def _run(self, call: Any) -> Any:
        usable = self._usable
        if not usable:
            raise LLMConfigurationError(
                "No model provider is configured. Check the provider settings and local-only mode."
            )
        failures = 0
        last: LLMGenerationError | None = None
        for member in usable:
            try:
                result = call(member)
            except LLMGenerationError as exc:
                failures += 1
                last = exc
                continue
            report_fallbacks(failures)
            return result
        raise LLMGenerationError("All configured model providers failed.") from last
