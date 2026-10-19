"""Bind trusted provider pricing to the planning cost estimate (ADR 0024, INV-4 S2b).

The planning estimate is the existing ``estimate_cost`` applied to the fixed ``PLANNING_LIMITS``;
this module only decides which ``ProviderCostProfile`` values it sees:

- every member of the provider envelope is eligible (fallbacks included), so an envelope member
  without an exact provider/model pricing entry makes the estimate ``unavailable(pricing_unknown)``,
  and an empty or absent envelope makes it ``unavailable(no_eligible_provider)``;
- prices come only from the trusted, versioned ``ProviderPricingTable``, never from the model,
  the planning context, the authority, or observed billing;
- enforcement comes only from trusted ``ReviewedPlanningEvidence``: a member counts as capped,
  input-bounded and retry-metered only if a current review covers exactly that member, so neither a
  pricing entry's flags nor an envelope's self-declared bounds can make an estimate an
  ``enforced_upper_bound``. A priced member without such a review is ``indicative`` with all three
  enforcement limitations; an unpriced one is still ``unavailable(pricing_unknown)``;
- pricing-entry flags are conjoined with the review, so a price entry can lower the bound but
  never raise it.

The generic ``estimate_cost`` is unchanged: legacy callers that pass ``ProviderCostProfile`` values
directly keep their semantics, and only this planning binding consults reviewed evidence. A later
admission step should reuse the same ``ReviewedPlanningEvidence.covering`` record.

The estimate uses no provider and consumes no model budget. It is never part of the plan, so a
pricing change cannot alter a plan digest or its approval.
"""

from __future__ import annotations

from pydantic import ValidationError

from packages.investigation.budget import (
    PLANNING_LIMITS,
    CostEstimate,
    ProviderCostProfile,
    ProviderPricingTable,
    estimate_cost,
)
from packages.investigation.gateway import (
    ProviderAttemptBound,
    ProviderEnvelope,
    ReviewedPlanningEvidence,
)


class CostInputInvalid(ValueError):
    """A pricing, envelope or evidence value that bypassed validation. Fixed-code message."""

    def __init__(self) -> None:
        super().__init__("cost_input_invalid")


def estimate_planning_cost(
    envelope: ProviderEnvelope | None,
    pricing: ProviderPricingTable | None,
    evidence: ReviewedPlanningEvidence | None = None,
) -> CostEstimate:
    """The planning-phase estimate for ``envelope`` under ``pricing``; see the module docstring.

    The S2 gateway passes the admitted envelope, or ``None`` when no adapter is admitted, and the
    trusted reviewed evidence, or ``None`` when no review exists (never ``enforced_upper_bound``).
    """
    envelope = _revalidated(envelope, ProviderEnvelope)
    pricing = _revalidated(pricing, ProviderPricingTable)
    evidence = _revalidated(evidence, ReviewedPlanningEvidence)
    members = () if envelope is None else envelope.members
    profiles = [_effective_profile(member, pricing, evidence) for member in members]
    return estimate_cost(
        PLANNING_LIMITS,
        profiles,
        price_table_version=None if pricing is None else pricing.version,
    )


def _effective_profile(
    member: ProviderAttemptBound,
    pricing: ProviderPricingTable | None,
    evidence: ReviewedPlanningEvidence | None,
) -> ProviderCostProfile:
    entry = (
        None
        if pricing is None
        else pricing.profile_for(provider=member.provider, model=member.model)
    )
    if entry is None:
        return ProviderCostProfile(provider=member.provider, model=member.model)
    # The review's validator fixes one attempt, a bounded prompt, and the stage output cap, and
    # ``covering`` requires the member to equal that bound; the member checks are defence in depth.
    reviewed = evidence is not None and evidence.covering(member) is not None
    return ProviderCostProfile(
        provider=member.provider,
        model=member.model,
        input_per_million_usd=entry.input_per_million_usd,
        output_per_million_usd=entry.output_per_million_usd,
        output_cap_enforced=(
            reviewed and entry.output_cap_enforced and member.max_output_tokens is not None
        ),
        input_bounded_before_call=(
            reviewed and entry.input_bounded_before_call and member.input_bounded
        ),
        retries_metered=reviewed and entry.retries_metered and member.max_attempts is not None,
    )


def _revalidated[T: (ProviderEnvelope, ProviderPricingTable, ReviewedPlanningEvidence)](
    value: T | None, kind: type[T]
) -> T | None:
    """Re-check a trusted value so a ``model_construct`` bypass fails closed, not silently."""
    if value is None:
        return None
    if type(value) is not kind:
        raise CostInputInvalid()
    try:
        # ``warnings=False``: a forged field would otherwise be quoted in a serializer warning.
        return kind.model_validate(value.model_dump(warnings=False))
    except (ValidationError, TypeError, ValueError, AttributeError):
        raise CostInputInvalid() from None
