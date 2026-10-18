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

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from packages.investigation.gateway import (
    PLANNING_STAGE_OUTPUT_TOKENS,
    CancellationToken,
    MeteringUnbounded,
    ProviderAttemptBound,
    ProviderEnvelope,
    ReviewedPlanningCapability,
)
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


class DeadlineTermination(StrEnum):
    """Provider-side disposition, distinct from merely stopping a local wait."""

    COMPLETED = "completed"
    PROVIDER_CANCELLED = "provider_cancelled"
    PROVIDER_DEADLINE_ENFORCED = "provider_deadline_enforced"


@dataclass(frozen=True)
class PlanningDeadline:
    """Positive duration remaining on the ledger's original monotonic deadline."""

    remaining_seconds: float

    def __post_init__(self) -> None:
        value = self.remaining_seconds
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise ValueError("remaining_seconds must be a finite number")
        if value <= 0:
            raise ValueError("remaining_seconds must be positive")
        object.__setattr__(self, "remaining_seconds", float(value))


@dataclass(frozen=True)
class DeadlineModelResult:
    """Content-bearing adapter result kept inside the future trusted planning gateway.

    ``termination_confirmed`` and ``provider_execution_bound_enforced`` are observations of the
    call outcome. False values remain representable so the gateway can classify a violation; they
    are never upgraded merely because an adapter implements this interface.
    """

    response: LLMResponse | None
    termination: DeadlineTermination
    cancellation_requested: bool
    termination_confirmed: bool
    provider_execution_bound_enforced: bool

    def __post_init__(self) -> None:
        flags = (
            self.cancellation_requested,
            self.termination_confirmed,
            self.provider_execution_bound_enforced,
        )
        if any(type(flag) is not bool for flag in flags):
            raise ValueError("deadline result flags must be booleans")
        if not isinstance(self.termination, DeadlineTermination):
            raise ValueError("unknown deadline termination")
        completed = self.termination is DeadlineTermination.COMPLETED
        if completed != isinstance(self.response, LLMResponse):
            raise ValueError("only a completed deadline result carries a response")


class DeadlineBoundedModelAdapter(BoundedModelAdapter):
    """One-call adapter accepting the ledger's remaining deadline.

    Implementing this class is not proof of provider-side enforcement. Admission also requires a
    separately constructed ``ReviewedPlanningCapability`` whose evidence is reviewed for the exact
    provider/model. Production remains fail closed until both exist.
    """

    @abstractmethod
    def generate_once_with_deadline(
        self,
        prompt: str,
        *,
        max_output_tokens: int,
        deadline: PlanningDeadline,
        cancellation: CancellationToken,
    ) -> DeadlineModelResult:
        """Make one request without extending ``deadline`` and confirm its termination state."""


def admit_planning_adapter(
    adapter: object,
    *,
    envelope: ProviderEnvelope,
    capability: ReviewedPlanningCapability | None,
) -> DeadlineBoundedModelAdapter:
    """Admit only an exact adapter/envelope/review tuple for the fixed planning stage cap."""
    try:
        if not isinstance(adapter, DeadlineBoundedModelAdapter):
            raise MeteringUnbounded()
        if not isinstance(capability, ReviewedPlanningCapability):
            raise MeteringUnbounded()
        if not isinstance(envelope, ProviderEnvelope) or len(envelope.members) != 1:
            raise MeteringUnbounded()
        bound = adapter.bound
        if (
            not isinstance(bound, ProviderAttemptBound)
            or bound != envelope.members[0]
            or bound != capability.bound
            or bound.max_attempts != 1
            or bound.input_bounded is not True
            or bound.max_output_tokens != PLANNING_STAGE_OUTPUT_TOKENS
            or capability.provider_deadline_enforced is not True
            or capability.termination_confirmed is not True
        ):
            raise MeteringUnbounded()
    except MeteringUnbounded:
        raise
    except Exception:
        raise MeteringUnbounded() from None
    return adapter
