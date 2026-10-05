"""Provider-neutral LLM interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel


class LLMConfigurationError(RuntimeError):
    """Raised when an LLM provider is not configured."""


class LLMGenerationError(RuntimeError):
    """Raised when an LLM provider fails to generate a response."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    provider: str


class LLMClient(Protocol):
    provider: str
    model: str

    def generate(self, prompt: str) -> LLMResponse:
        """Generate text from a prompt."""


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class StructuredLLMClient(Protocol):
    def generate_structured(self, prompt: str, schema_model: type[SchemaT]) -> SchemaT:
        """Generate and validate a structured response."""
