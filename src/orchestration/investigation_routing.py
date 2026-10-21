"""Pure contracts for the feature-gated investigation proposal route (ADR 0024 Amendment 7).

This module is deliberately standard-library only.  The eligibility predicate added in INV-9 may
accept exactly the current question and two trusted booleans; it must not acquire a model, history,
retriever, resource identifier, or arbitrary context channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

INVESTIGATION_ROUTE_VERSION = "inv9-routing-v1"
INVESTIGATION_PROPOSAL_SCHEMA_VERSION = 1

InvestigationIntentReason = Literal[
    "explicit_investigation",
    "metric_change_causal",
    "multi_part_analysis",
]
InvestigationEligibilityReason = Literal[
    "feature_disabled",
    "authorized_table_missing",
    "intent_not_eligible",
    "explicit_investigation",
    "metric_change_causal",
    "multi_part_analysis",
]
InvestigationProposalReason = Literal[
    "feature_enabled",
    "authorized_table_present",
    "explicit_investigation",
    "metric_change_causal",
    "multi_part_analysis",
]

_INTENT_REASONS = frozenset(
    {"explicit_investigation", "metric_change_causal", "multi_part_analysis"}
)


@dataclass(frozen=True)
class InvestigationEligibilityDecision:
    """Content-free result of the deterministic gate."""

    eligible: bool
    primary_reason: InvestigationEligibilityReason
    reason_codes: tuple[InvestigationProposalReason, ...] = ()

    def __post_init__(self) -> None:
        if not self.eligible:
            if self.reason_codes or self.primary_reason in _INTENT_REASONS:
                raise ValueError(
                    "an ineligible decision has one failure reason and no proposal codes"
                )
            return
        if self.primary_reason not in _INTENT_REASONS:
            raise ValueError("an eligible decision requires an investigation-intent reason")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("proposal reason codes must be unique")
        if self.reason_codes[:2] != ("feature_enabled", "authorized_table_present"):
            raise ValueError("eligible reasons must prove the feature and authorized table gates")
        intent_reasons = self.reason_codes[2:]
        if not intent_reasons or any(reason not in _INTENT_REASONS for reason in intent_reasons):
            raise ValueError("eligible reasons must include only reviewed intent codes")
        if self.primary_reason not in intent_reasons:
            raise ValueError("the primary reason must be preserved in the proposal reasons")
