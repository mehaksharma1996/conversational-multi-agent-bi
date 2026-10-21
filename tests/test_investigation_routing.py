"""Deterministic investigation-routing contracts (INV-9)."""

from __future__ import annotations

import pytest

from src.orchestration.investigation_routing import InvestigationEligibilityDecision


def test_eligible_decision_preserves_ordered_fixed_reasons() -> None:
    decision = InvestigationEligibilityDecision(
        eligible=True,
        primary_reason="metric_change_causal",
        reason_codes=(
            "feature_enabled",
            "authorized_table_present",
            "metric_change_causal",
            "multi_part_analysis",
        ),
    )

    assert decision.reason_codes[-2:] == ("metric_change_causal", "multi_part_analysis")


@pytest.mark.parametrize(
    ("primary_reason", "reason_codes"),
    [
        ("feature_disabled", ("feature_enabled", "authorized_table_present")),
        ("metric_change_causal", ()),
    ],
)
def test_ineligible_decision_rejects_proposal_metadata(primary_reason, reason_codes) -> None:
    with pytest.raises(ValueError):
        InvestigationEligibilityDecision(
            eligible=False,
            primary_reason=primary_reason,
            reason_codes=reason_codes,
        )


@pytest.mark.parametrize(
    "reason_codes",
    [
        ("authorized_table_present", "metric_change_causal"),
        ("feature_enabled", "authorized_table_present"),
        (
            "feature_enabled",
            "authorized_table_present",
            "metric_change_causal",
            "metric_change_causal",
        ),
    ],
)
def test_eligible_decision_requires_coherent_minimum_metadata(reason_codes) -> None:
    with pytest.raises(ValueError):
        InvestigationEligibilityDecision(
            eligible=True,
            primary_reason="metric_change_causal",
            reason_codes=reason_codes,
        )
