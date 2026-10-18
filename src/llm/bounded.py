"""The model-client contract the investigation tool gateway requires (ADR 0024, #136 S5).

The gateway reserves the worst case of a model call *before* the call, so it needs guarantees that
make the reservation an enforceable bound rather than an estimate:

- **identity**: one fixed provider and model for every call: no purpose-tier routing, no fallback;
- **one attempt**: exactly one provider request per call, with no retry in the client, its SDK, or
  its transport;
- **an exact input count**: the input tokens the provider will bill for the *complete* request
  (system instruction, chat template, and prompt), computed before anything is sent;
- **an enforced output cap**: the cap is sent with the request and the provider bills no more;
- **reporting**: after a call, ``report_usage`` with both token counts and ``retries=0``, and
  ``report_served_by`` with the declared provider and model.

``BoundedModelAdapter`` states that contract. Subclassing it is a reviewed, code-level claim; the
gateway still checks every claim it can (identity against the provider envelope, the input count
before the call, and every report after it) and fails closed on any mismatch.

Admission proves only that the adapter's declaration matches the authority's envelope; it is not
evidence that any call stayed within budget (``BudgetUsage.reservation_bound`` is ``held`` only
after a complete, in-reservation report). ``count_input_tokens`` must not make a billable request.

Current production providers remain unsupported for model-executing investigation tools: none of
the existing clients meets the contract, so none is an adapter and investigation model calls are
refused with ``metering_unbounded``:

- ``GeminiClient``: routes the model by purpose tier, retries internally (``max_retries=2``), sets
  no output cap unless configured, adds a system instruction outside the prompt, and its tokenizer
  is not available locally, so the input cannot be counted exactly before the call;
- ``AnthropicClient``: routes by purpose tier, retries internally, and adds a system instruction;
  its input is not counted before the call;
- ``OllamaClient``: routes by purpose tier, retries internally, sends no output cap, and the
  server's model template (operator configuration) wraps the prompt with text the client cannot
  see;
- ``FallbackLLMClient``: may serve a call from any member.

UTF-8 byte length is not a substitute for an exact count: the provider adds tokens around the
prompt that the prompt's bytes do not contain, and a tokenizer that normalises text (for example
NFKC, where the 3-byte U+FDFA expands to 18 characters, 33 bytes) can produce more tokens than
bytes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from packages.investigation.gateway import ProviderAttemptBound
from src.llm.base import LLMResponse


class BoundedModelAdapter(ABC):
    """A single-provider model client whose limits the gateway can enforce. See the module doc."""

    @property
    @abstractmethod
    def bound(self) -> ProviderAttemptBound:
        """This adapter's envelope member: ``max_attempts=1``, its output cap, ``input_bounded``."""

    @property
    def provider(self) -> str:
        return self.bound.provider

    @property
    def model(self) -> str:
        return self.bound.model

    @abstractmethod
    def count_input_tokens(self, prompt: str) -> int:
        """The exact input tokens the provider will bill for the complete request for ``prompt``."""

    @abstractmethod
    def generate_once(self, prompt: str, *, max_output_tokens: int) -> LLMResponse:
        """Send exactly one request with ``max_output_tokens`` as the provider-enforced cap."""
