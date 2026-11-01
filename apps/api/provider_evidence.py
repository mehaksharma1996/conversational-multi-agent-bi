"""Typed, versioned, review-bound provider evidence and price review (PROD-1 S5c, #198).

One ``ReviewedProviderEvidence`` bundle holds everything admission needs about one exact provider
snapshot, under one review ID: the adapter capability profile, the planning and execution
evidence, the pricing table the gateways estimate with, and a ``ReviewedPriceEvidence`` record that
names every billable component. Admission (``apps.api.investigation_providers``) looks bundles up by
the exact snapshot, re-validates them, and calls ``evidence_refusal``.

Constructing a bundle proves only its shape. ``evidence_refusal`` checks the substance and returns
the first failing check as a content-free ``EvidenceRefusal``:

- the schema version is supported;
- provider, snapshot, and consent recipient agree across the bundle, the capability profile, the
  planning and execution reviews, the pricing table, and the price review (no alias or family);
- every part carries the same review ID;
- the review is current (``reviewed_on <= today < expires_on``);
- each review holds exactly one member, equal to the fixed planning or execution bound;
- the pricing table has exactly one entry, with both rates equal to the price review and every
  enforcement flag set (otherwise an estimate cannot be an enforced upper bound);
- the price review names exactly the supported billable components, and no component is unknown.
  The input-token count request is not assumed free: it must be reviewed as ``not_billed``. A
  count that is billed (or unknown) is refused, because the planning estimate prices input and
  output tokens only, so a billed count would leave the upper bound short.

A record is evidence of review, not of provider behaviour: the gateways still check every count,
report, termination, and identity at runtime. Production holds no bundle
(``REVIEWED_PROVIDER_EVIDENCE`` is empty); tests build synthetic ones.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from packages.investigation.budget import Price, ProviderPricingTable
from packages.investigation.gateway import (
    PLANNING_STAGE_OUTPUT_TOKENS,
    ProviderAttemptBound,
    ReviewedExecutionEvidence,
    ReviewedPlanningEvidence,
)
from src.llm.openai_responses import OPENAI_DATA_RECIPIENT, OPENAI_PROVIDER
from src.llm.openai_responses_bounded import ResponsesCapabilityProfile

SUPPORTED_EVIDENCE_SCHEMAS = frozenset({1})
EXECUTION_CALL_OUTPUT_TOKENS = 1_000
INPUT_TOKENS = "input_tokens"
OUTPUT_TOKENS = "output_tokens"
INPUT_TOKEN_COUNT = "input_token_count"
SUPPORTED_BILLING_COMPONENTS = frozenset({INPUT_TOKENS, OUTPUT_TOKENS, INPUT_TOKEN_COUNT})

ReviewId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.\-]{0,63}$")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class EvidenceRefusal(StrEnum):
    """Why a bundle cannot support admission. Content-free by construction."""

    INVALID = "evidence_invalid"
    SCHEMA_UNSUPPORTED = "evidence_schema_unsupported"
    IDENTITY_MISMATCH = "evidence_identity_mismatch"
    REVIEW_MISMATCH = "review_mismatch"
    EXPIRED = "evidence_expired"
    PLANNING_UNREVIEWED = "planning_evidence_unreviewed"
    EXECUTION_UNREVIEWED = "execution_evidence_unreviewed"
    PRICING_UNREVIEWED = "pricing_unreviewed"
    PRICING_UNBOUNDED = "pricing_unbounded"


class ReviewedPriceEvidence(BaseModel):
    """The reviewed billing basis for one exact snapshot.

    ``billable_components`` must name every component the provider can bill for one logical call.
    ``count_charge`` is ``not_billed``, ``billed`` (with ``count_per_million_usd``), or
    ``unknown``; only ``not_billed`` is admissible today (module docstring).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: Name
    model: Name
    review_id: ReviewId
    currency: Literal["USD"]
    input_per_million_usd: Price
    output_per_million_usd: Price
    billable_components: tuple[str, ...] = Field(min_length=1, max_length=8)
    count_charge: Literal["not_billed", "billed", "unknown"]
    count_per_million_usd: Price | None = None


class ReviewedProviderEvidence(BaseModel):
    """Everything admission needs about one exact snapshot, under one review ID."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int
    review_id: ReviewId
    provider: Name
    model: Name
    data_recipient: Name
    source: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.:/\-]{0,127}$")]
    reviewed_on: date
    expires_on: date
    capabilities: ResponsesCapabilityProfile
    planning_evidence: ReviewedPlanningEvidence
    execution_evidence: ReviewedExecutionEvidence
    pricing: ProviderPricingTable
    price: ReviewedPriceEvidence

    def bound(self, cap: int) -> ProviderAttemptBound:
        return ProviderAttemptBound(
            provider=self.provider,
            model=self.model,
            data_recipient=self.data_recipient,
            max_attempts=1,
            max_output_tokens=cap,
            input_bounded=True,
        )


_PARTS = {
    "capabilities": ResponsesCapabilityProfile,
    "planning_evidence": ReviewedPlanningEvidence,
    "execution_evidence": ReviewedExecutionEvidence,
    "pricing": ProviderPricingTable,
    "price": ReviewedPriceEvidence,
}


def revalidated(evidence: object) -> ReviewedProviderEvidence | None:
    """An exact-type, freshly validated copy, or ``None``: a forged or bypassed object fails."""
    if type(evidence) is not ReviewedProviderEvidence:
        return None
    if any(type(getattr(evidence, name, None)) is not kind for name, kind in _PARTS.items()):
        return None
    try:
        return ReviewedProviderEvidence.model_validate(evidence.model_dump())
    except Exception:
        return None


def evidence_refusal(evidence: ReviewedProviderEvidence, today: date) -> EvidenceRefusal | None:
    """The first substantive check ``evidence`` fails (module docstring), or ``None``."""
    if evidence.schema_version not in SUPPORTED_EVIDENCE_SCHEMAS:
        return EvidenceRefusal.SCHEMA_UNSUPPORTED
    provider, model, review = evidence.provider, evidence.model, evidence.review_id
    if (
        provider != OPENAI_PROVIDER
        or evidence.data_recipient != OPENAI_DATA_RECIPIENT
        or evidence.capabilities.profile.model != model
        or (evidence.price.provider, evidence.price.model) != (provider, model)
    ):
        return EvidenceRefusal.IDENTITY_MISMATCH
    if (
        evidence.capabilities.review != review
        or evidence.planning_evidence.evidence_version != review
        or evidence.execution_evidence.evidence_version != review
        or evidence.pricing.version != review
        or evidence.price.review_id != review
    ):
        return EvidenceRefusal.REVIEW_MISMATCH
    if evidence.expires_on <= evidence.reviewed_on or today < evidence.reviewed_on:
        return EvidenceRefusal.INVALID
    if today >= evidence.expires_on:
        return EvidenceRefusal.EXPIRED
    planning = evidence.planning_evidence
    if (
        len(planning.capabilities) != 1
        or planning.covering(evidence.bound(PLANNING_STAGE_OUTPUT_TOKENS)) is None
    ):
        return EvidenceRefusal.PLANNING_UNREVIEWED
    execution = evidence.execution_evidence
    if (
        len(execution.capabilities) != 1
        or execution.covering(evidence.bound(EXECUTION_CALL_OUTPUT_TOKENS)) is None
    ):
        return EvidenceRefusal.EXECUTION_UNREVIEWED
    pricing, price = evidence.pricing, evidence.price
    entry = pricing.profile_for(provider=provider, model=model)
    if (
        len(pricing.profiles) != 1
        or entry is None
        or entry.input_per_million_usd != price.input_per_million_usd
        or entry.output_per_million_usd != price.output_per_million_usd
    ):
        return EvidenceRefusal.PRICING_UNREVIEWED
    components = set(price.billable_components)
    if (
        not (entry.output_cap_enforced and entry.input_bounded_before_call)
        or not entry.retries_metered
        or len(components) != len(price.billable_components)
        or not components <= SUPPORTED_BILLING_COMPONENTS
        or not {INPUT_TOKENS, OUTPUT_TOKENS} <= components
        or price.count_charge != "not_billed"
        or INPUT_TOKEN_COUNT in components
        or price.count_per_million_usd is not None
    ):
        return EvidenceRefusal.PRICING_UNBOUNDED
    return None


__all__ = [
    "EXECUTION_CALL_OUTPUT_TOKENS",
    "SUPPORTED_BILLING_COMPONENTS",
    "SUPPORTED_EVIDENCE_SCHEMAS",
    "EvidenceRefusal",
    "ReviewedPriceEvidence",
    "ReviewedProviderEvidence",
    "evidence_refusal",
    "revalidated",
]
