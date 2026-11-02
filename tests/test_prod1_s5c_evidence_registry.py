"""PROD-1 S5c: one typed, versioned, review-bound evidence bundle per exact snapshot.

Every bundle here is SYNTHETIC and test-only: its prices are not real or third-party OpenAI
prices, and its flags record no real review. Production registries stay empty, the release gate
stays closed, and no provider is admitted. No network, key, or provider account is used.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from apps.api import investigation_providers as providers
from apps.api.investigation_providers import (
    AdmissionDecision,
    ConfiguredInvestigationAdapters,
    ReviewedProviderConfig,
    admit_investigation_provider,
)
from apps.api.provider_evidence import (
    EvidenceRefusal,
    ReviewedProviderEvidence,
    evidence_refusal,
    revalidated,
)
from packages.investigation.budget import ProviderCostProfile, ProviderPricingTable
from packages.investigation.gateway import PLANNING_STAGE_OUTPUT_TOKENS
from packages.investigation.planning_cost import estimate_planning_cost
from tests.test_openai_responses_bounded import FAKE_KEY, SNAPSHOT, Clock, Provider, capabilities
from tests.test_prod1_configured_adapters import (
    REVIEW,
    bundle,
    configured,
    reviewed_evidence,
    reviewed_execution,
    reviewed_price,
    reviewed_pricing,
)
from tests.test_prod1_gateway_integration import (
    EXEC_BOUND,
    PLAN_BOUND,
    _no_network,  # noqa: F401 - autouse: any non-loopback connection fails the test
)

OTHER = "other-review-2"
ALIAS = "test-model-2099-02-02"
TODAY = datetime.now(UTC).date()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


def admit(root: Path, registry: Any = None, **settings: Any) -> AdmissionDecision:
    chosen = {SNAPSHOT: bundle()} if registry is None else registry
    return admit_investigation_provider(configured(root, **settings), evidence=chosen).decision


def refusal(**changes: Any) -> EvidenceRefusal | None:
    return evidence_refusal(bundle(**changes), TODAY)


def priced_table(**profile: Any) -> ProviderPricingTable:
    values: dict[str, Any] = {
        "provider": "openai",
        "model": SNAPSHOT,
        "input_per_million_usd": 0.4,
        "output_per_million_usd": 1.6,
        "output_cap_enforced": True,
        "input_bounded_before_call": True,
        "retries_metered": True,
    }
    values.update(profile)
    return ProviderPricingTable(
        version=REVIEW, currency="USD", profiles=(ProviderCostProfile(**values),)
    )


# --- 1: consistent evidence, and the sequence it completes -----------------------------------


def test_consistent_synthetic_evidence_passes_every_evidence_check(root: Path) -> None:
    assert refusal() is None
    assert admit(root) is AdmissionDecision.LIVE_VALIDATION_REQUIRED  # only the gate remains


def test_with_the_gate_opened_the_admitted_estimate_is_an_enforced_upper_bound(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(providers, "LIVE_ADMISSION_RELEASED", True)
    admission = admit_investigation_provider(
        configured(root), evidence={SNAPSHOT: bundle()}, today=TODAY
    )
    assert admission.decision is AdmissionDecision.ADMITTED and admission.config is not None
    built = ConfiguredInvestigationAdapters(admission.config, transport_factory=lambda: None)
    planning = built.planning()
    estimate = estimate_planning_cost(planning.envelope, planning.pricing, planning.evidence)
    assert estimate.bound == "enforced_upper_bound"
    assert estimate.amount_usd is not None and estimate.amount_usd > 0
    assert estimate.price_table_version == REVIEW


# --- 2-5, 9: missing records and alias lookup ------------------------------------------------


@pytest.mark.parametrize("part", ["capabilities", "planning_evidence", "execution_evidence"])
def test_a_bundle_cannot_omit_a_review(part: str) -> None:
    with pytest.raises(ValidationError):
        bundle(**{part: None})


@pytest.mark.parametrize("part", ["pricing", "price"])
def test_a_bundle_cannot_omit_pricing(part: str) -> None:
    with pytest.raises(ValidationError):
        bundle(**{part: None})


@pytest.mark.parametrize(
    "registry",
    [{}, {"test-model": bundle()}, {"gpt-4.1-mini": bundle()}],
    ids=["empty", "family-alias", "other-family-alias"],
)
def test_no_bundle_for_the_exact_snapshot_refuses(root: Path, registry: Any) -> None:
    assert admit(root, registry) is AdmissionDecision.CAPABILITY_UNREVIEWED


def test_a_bundle_filed_under_another_snapshot_key_is_refused(root: Path) -> None:
    registry = {ALIAS: bundle()}  # the bundle describes SNAPSHOT
    assert admit(root, registry, investigation_model_snapshot=ALIAS) is (
        AdmissionDecision.EVIDENCE_IDENTITY_MISMATCH
    )


# --- 6-8: one review ID, one identity ---------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"capabilities": capabilities(review=OTHER)},
        {"planning_evidence": reviewed_evidence(OTHER)},
        {"execution_evidence": reviewed_execution(OTHER)},
        {"pricing": reviewed_pricing(version=OTHER)},
        {"price": reviewed_price(review_id=OTHER)},
        {"review_id": OTHER},
    ],
    ids=["profile", "planning", "execution", "pricing", "price", "bundle"],
)
def test_every_part_must_carry_the_same_review_id(changes: dict[str, Any]) -> None:
    assert refusal(**changes) is EvidenceRefusal.REVIEW_MISMATCH


def test_the_configured_review_id_must_match_the_bundle(root: Path) -> None:
    assert admit(root, investigation_model_review=OTHER) is AdmissionDecision.REVIEW_MISMATCH
    assert admit(root, investigation_model_review=None) is AdmissionDecision.REVIEW_MISMATCH


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "azure"},
        {"data_recipient": "Azure OpenAI"},
        {"model": ALIAS},
        {"price": reviewed_price(provider="azure")},
        {"price": reviewed_price(model=ALIAS)},
        {
            "capabilities": capabilities(
                profile=capabilities().profile.model_copy(update={"model": ALIAS})
            )
        },
    ],
    ids=["provider", "recipient", "snapshot", "price-provider", "price-snapshot", "profile"],
)
def test_provider_snapshot_and_recipient_must_agree_everywhere(changes: dict[str, Any]) -> None:
    assert refusal(**changes) is EvidenceRefusal.IDENTITY_MISMATCH


def test_planning_and_execution_reviews_must_cover_the_exact_bounds() -> None:
    other_model = PLAN_BOUND.model_copy(update={"model": ALIAS})
    assert refusal(planning_evidence=reviewed_planning(other_model)) is (
        EvidenceRefusal.PLANNING_UNREVIEWED
    )
    assert refusal(execution_evidence=reviewed_execution(bound=PLAN_BOUND)) is (
        EvidenceRefusal.EXECUTION_UNREVIEWED  # the planning cap, not the execution cap
    )
    wider = EXEC_BOUND.model_copy(update={"max_output_tokens": 4_000})
    assert refusal(execution_evidence=reviewed_execution(bound=wider)) is (
        EvidenceRefusal.EXECUTION_UNREVIEWED
    )


def reviewed_planning(bound: Any) -> Any:
    from packages.investigation.gateway import (
        ReviewedPlanningCapability,
        ReviewedPlanningEvidence,
    )

    return ReviewedPlanningEvidence(
        evidence_version=REVIEW,
        capabilities=(
            ReviewedPlanningCapability(
                bound=bound,
                evidence_version=REVIEW,
                provider_deadline_enforced=True,
                termination_confirmed=True,
            ),
        ),
    )


# --- 10-13: schema, expiry, duplicates, forgery -----------------------------------------------


def test_an_unsupported_schema_version_is_refused() -> None:
    assert refusal(schema_version=2) is EvidenceRefusal.SCHEMA_UNSUPPORTED
    assert refusal(schema_version=0) is EvidenceRefusal.SCHEMA_UNSUPPORTED
    with pytest.raises(ValidationError):
        bundle(schema_version="1")
    with pytest.raises(ValidationError):
        bundle(schema_version=True)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"expires_on": TODAY}, EvidenceRefusal.EXPIRED),
        (
            {"reviewed_on": TODAY - timedelta(days=10), "expires_on": TODAY - timedelta(days=1)},
            EvidenceRefusal.EXPIRED,
        ),
        ({"reviewed_on": TODAY + timedelta(days=1)}, EvidenceRefusal.INVALID),  # from the future
        (
            {"reviewed_on": TODAY - timedelta(days=1), "expires_on": TODAY - timedelta(days=1)},
            EvidenceRefusal.INVALID,
        ),
    ],
)
def test_expired_or_impossibly_dated_evidence_is_refused(
    changes: dict[str, Any], expected: EvidenceRefusal
) -> None:
    assert refusal(**changes) is expected


def test_expired_evidence_never_reaches_the_gate(root: Path) -> None:
    stale = {SNAPSHOT: bundle(expires_on=TODAY)}
    assert admit(root, stale) is AdmissionDecision.EVIDENCE_EXPIRED


def test_duplicate_or_conflicting_records_are_refused() -> None:
    two = ProviderPricingTable(
        version=REVIEW,
        currency="USD",
        profiles=(
            *reviewed_pricing().profiles,
            reviewed_pricing(ALIAS).profiles[0],
        ),
    )
    assert refusal(pricing=two) is EvidenceRefusal.PRICING_UNREVIEWED
    assert refusal(pricing=priced_table(input_per_million_usd=0.5)) is (
        EvidenceRefusal.PRICING_UNREVIEWED  # the table and the price review disagree
    )
    doubled = ("input_tokens", "output_tokens", "output_tokens")
    assert refusal(price=reviewed_price(billable_components=doubled)) is (
        EvidenceRefusal.PRICING_UNBOUNDED
    )


def test_forged_or_substituted_bundles_fail_closed(root: Path) -> None:
    forged = ReviewedProviderEvidence.model_construct(
        **{
            **dict(bundle()),
            "price": reviewed_price().model_copy(update={"input_per_million_usd": -1.0}),
        }
    )
    assert revalidated(forged) is None
    assert admit(root, {SNAPSHOT: forged}) is AdmissionDecision.EVIDENCE_INVALID

    class Lenient(ReviewedProviderEvidence):
        pass

    lenient = Lenient(**dict(bundle()))
    assert admit(root, {SNAPSHOT: lenient}) is AdmissionDecision.EVIDENCE_INVALID
    with pytest.raises(TypeError):
        ReviewedProviderConfig(evidence=lenient, api_key=FAKE_KEY)
    assert admit(root, {SNAPSHOT: object()}) is AdmissionDecision.EVIDENCE_INVALID


# --- 14-17, 23: reviewed flags are exact ------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"background_mode": False},
        {"provider_cancel": 1},
        {"usage_on_completion": None},
        {"exact_input_count": "true"},
    ],
)
def test_capability_profile_flags_must_be_exactly_true(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        capabilities(**changes)


@pytest.mark.parametrize(
    "profile",
    [
        {"output_cap_enforced": 1},
        {"retries_metered": "yes"},
        {"input_bounded_before_call": 1.0},
    ],
)
def test_pricing_flags_reject_integers_and_strings(profile: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        priced_table(**profile)


@pytest.mark.parametrize(
    "profile",
    [
        {"output_cap_enforced": False},
        {"input_bounded_before_call": False},
        {"retries_metered": False},
    ],
)
def test_an_unenforced_pricing_flag_cannot_support_an_upper_bound(profile: dict[str, Any]) -> None:
    assert refusal(pricing=priced_table(**profile)) is EvidenceRefusal.PRICING_UNBOUNDED


# --- 18-22: prices and billable components -------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"input_per_million_usd": None},
        {"output_per_million_usd": None},
        {"input_per_million_usd": -0.1},
        {"output_per_million_usd": math.nan},
        {"input_per_million_usd": math.inf},
        {"output_per_million_usd": "1.6"},
        {"input_per_million_usd": True},
        {"currency": "EUR"},
        {"unexpected": 1},
    ],
)
def test_unknown_negative_or_malformed_prices_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        reviewed_price(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"count_charge": "unknown"},
        {"count_charge": "billed", "count_per_million_usd": 0.1},
        {"count_charge": "billed"},
        {
            "count_charge": "billed",
            "billable_components": ("input_tokens", "output_tokens", "input_token_count"),
            "count_per_million_usd": 0.1,
        },
        {"billable_components": ("input_tokens", "output_tokens", "input_token_count")},
        {"count_per_million_usd": 0.0},
    ],
    ids=["unknown", "billed", "billed-unpriced", "billed-listed", "listed", "priced-not-billed"],
)
def test_the_count_request_is_never_assumed_free_or_left_unbounded(
    changes: dict[str, Any],
) -> None:
    assert refusal(price=reviewed_price(**changes)) is EvidenceRefusal.PRICING_UNBOUNDED


@pytest.mark.parametrize(
    "components",
    [
        ("input_tokens", "output_tokens", "reasoning_tokens"),
        ("input_tokens", "output_tokens", "background_storage"),
        ("input_tokens",),
        ("output_tokens",),
    ],
)
def test_unsupported_or_missing_billing_components_are_refused(components: tuple[str, ...]) -> None:
    assert refusal(price=reviewed_price(billable_components=components)) is (
        EvidenceRefusal.PRICING_UNBOUNDED
    )


def test_pricing_refusals_reach_admission(root: Path) -> None:
    unbounded = {SNAPSHOT: bundle(price=reviewed_price(count_charge="unknown"))}
    assert admit(root, unbounded) is AdmissionDecision.PRICING_UNBOUNDED


# --- 24-25: estimates are never understated, unknown usage holds the reservation ----------------


def test_estimates_without_full_evidence_are_never_an_upper_bound_or_zero() -> None:
    adapters = ConfiguredInvestigationAdapters(
        ReviewedProviderConfig(evidence=bundle(), api_key=FAKE_KEY),
        transport_factory=lambda: None,
    )
    envelope = adapters.planning().envelope
    unreviewed = estimate_planning_cost(envelope, reviewed_pricing(), None)
    assert unreviewed.bound == "indicative"
    unpriced = estimate_planning_cost(envelope, None, reviewed_evidence())
    assert unpriced.bound == "unavailable" and unpriced.amount_usd is None
    weak = estimate_planning_cost(
        envelope, priced_table(retries_metered=False), reviewed_evidence()
    )
    assert weak.bound != "enforced_upper_bound"


def test_unknown_usage_holds_the_full_reservation_and_is_never_repeated() -> None:
    from tests.test_openai_responses_bounded import counted
    from tests.test_prod1_gateway_integration import planning_run, refused

    run = planning_run(counted(1_500), httpx.ReadTimeout("create timed out"))
    result = run.initial()
    refused(result, "provider_failure")
    usage = result.usage
    assert (usage.input_tokens, usage.output_tokens) == (6_000, PLANNING_STAGE_OUTPUT_TOKENS)
    assert usage.unmetered_model_calls == 1 and run.adapter.halted


# --- 26-28: identity binding, scripted path, isolation -----------------------------------------


def test_built_adapters_carry_exactly_the_reviewed_identity() -> None:
    adapters = ConfiguredInvestigationAdapters(
        ReviewedProviderConfig(evidence=bundle(), api_key=FAKE_KEY),
        transport_factory=lambda: httpx.MockTransport(Provider(Clock())),
    )
    planning, execution = adapters.planning(), adapters.execution()
    assert planning.adapter.bound == PLAN_BOUND  # type: ignore[union-attr]
    assert execution.llm_client.bound == EXEC_BOUND  # type: ignore[union-attr]
    assert planning.evidence.covering(PLAN_BOUND) is not None  # type: ignore[union-attr]
    assert execution.evidence is not None and execution.evidence.covering(EXEC_BOUND)
    assert execution.scripted_admission is None
    assert planning.pricing.profile_for(provider="openai", model=SNAPSHOT) is not None  # type: ignore[union-attr]


def test_production_holds_no_evidence_and_admits_nothing(root: Path) -> None:
    assert dict(providers.REVIEWED_PROVIDER_EVIDENCE) == {}
    assert providers.LIVE_ADMISSION_RELEASED is False
    selection = providers.select_investigation_adapters(configured(root))
    assert selection.decision is AdmissionDecision.CAPABILITY_UNREVIEWED
    assert type(selection.adapters).__name__ == "FailClosedAdapters"


def test_refusal_codes_and_config_carry_no_key_or_review_content() -> None:
    config = ReviewedProviderConfig(evidence=bundle(), api_key=FAKE_KEY)
    assert FAKE_KEY not in repr(config)
    for code in EvidenceRefusal:
        assert REVIEW not in code.value and "synthetic" not in code.value
    assert {code.value for code in EvidenceRefusal} <= {d.value for d in AdmissionDecision}


def test_routing_is_off_and_no_credential_is_needed(root: Path) -> None:
    from tests.test_api_features import _settings

    settings = _settings(root)
    assert settings.investigation_routing_enabled is False
    assert settings.openai_api_key is None
    assert admit(root, investigation_model_activation=False) is AdmissionDecision.ACTIVATION_OFF
