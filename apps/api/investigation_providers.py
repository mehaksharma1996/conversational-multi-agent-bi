"""Trusted investigation provider selection and adapter ownership (PROD-1 S4, #198).

``select_investigation_adapters(settings)`` is the one place server configuration can turn into an
investigation model provider. It returns ``FailClosedAdapters`` unless every step of the
admission sequence passes, in this order, and reports the first failing step as a content-free
``AdmissionDecision``:

1. ``INVESTIGATION_MODEL_PROVIDER`` names a supported provider (default ``none``);
2. ``INVESTIGATION_MODEL_ACTIVATION`` is on (default off);
3. ``LOCAL_ONLY_MODE`` is off (a hosted provider is withheld, as for every other client);
4. ``OPENAI_API_KEY`` is present (its absence never falls back to another provider);
5. ``INVESTIGATION_MODEL_SNAPSHOT`` names a pinned dated snapshot;
6. that exact snapshot has a reviewed ``ResponsesCapabilityProfile``. There is no family or alias
   lookup, so a different snapshot cannot silently change limits, prices, or behaviour;
7. ``INVESTIGATION_MODEL_REVIEW`` equals the profile's and the planning evidence's review version;
8. reviewed planning evidence covers the exact planning bound;
9. a reviewed price entry exists for the exact provider and snapshot (``LLM_*_COST_*`` settings
   are never used);
10. the release gate ``LIVE_ADMISSION_RELEASED`` is open. It is a code constant, not a setting:
    no release has passed live validation (S6) or activation review (DEP-1), so it is ``False``.

S4 therefore admits nothing. The reviewed registries (``REVIEWED_CAPABILITY_PROFILES``,
``REVIEWED_PLANNING_EVIDENCE``, ``REVIEWED_PRICING``) are empty, and the gate is closed; any one
of these blocks admission on its own. Budget, consent, authorization and approvals, and the
operation deadline are not admission-time facts: the planning and tool gateways check them before
every model call, unchanged.

Selection does no I/O and builds no adapter or transport. Nothing a request carries reaches it:
the provider, snapshot, endpoint (fixed in ``src.llm.responses_transport``), and key come only
from the server environment.

**Ownership.** ``ConfiguredInvestigationAdapters`` builds a new ``OpenAIResponsesBoundedAdapter``
and ``ResponsesTransport`` on every ``planning()`` and ``execution()`` call and keeps no reference
to them. ``InvestigationRuntime`` makes each call once per investigation (the plan job and the
execute job), and a resume continues the same run, so one adapter instance serves every model
call of that stage: an uncertain create halts it for the rest of the investigation, and no
gateway, task retry, or resume obtains a fresh one. Distinct investigations, and so distinct
tenants, never share an instance. This guarantee relies on the single-process runtime: a restart
makes every live investigation ``interrupted`` and never rebuilds a run. It does not prevent
duplicates across processes or hosts.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import httpx

from apps.api.investigations import (
    MAX_EXECUTION_INPUT_TOKENS,
    ExecutionProvider,
    FailClosedAdapters,
    InvestigationAdapters,
    PlanningProvider,
)
from config.settings import Settings
from packages.investigation.budget import ProviderPricingTable
from packages.investigation.gateway import (
    PLANNING_STAGE_OUTPUT_TOKENS,
    ProviderAttemptBound,
    ProviderEnvelope,
    ReviewedPlanningEvidence,
)
from src.llm.openai_responses import OPENAI_DATA_RECIPIENT, OPENAI_PROVIDER
from src.llm.openai_responses_bounded import (
    REVIEWED_CAPABILITY_PROFILES,
    OpenAIResponsesBoundedAdapter,
    ResponsesCapabilityProfile,
)
from src.llm.providers import SYSTEM_INSTRUCTION
from src.llm.responses_transport import ResponsesTransport

OPENAI_RESPONSES = "openai_responses"
EXECUTION_CALL_OUTPUT_TOKENS = 1_000

# Admission step 10. Only a reviewed code change, after S6 and DEP-1, may open it.
LIVE_ADMISSION_RELEASED: Final = False

# Reviewed planning evidence and price entry for the configured snapshot (S5). None exist.
REVIEWED_PLANNING_EVIDENCE: ReviewedPlanningEvidence | None = None
REVIEWED_PRICING: ProviderPricingTable | None = None


class AdmissionDecision(StrEnum):
    """Content-free result of provider selection: the first failing step, or ``admitted``."""

    PROVIDER_DISABLED = "provider_disabled"
    PROVIDER_UNSUPPORTED = "provider_unsupported"
    ACTIVATION_OFF = "activation_off"
    LOCAL_ONLY = "local_only_mode"
    CREDENTIAL_MISSING = "credential_missing"
    SNAPSHOT_MISSING = "snapshot_missing"
    CAPABILITY_UNREVIEWED = "capability_unreviewed"
    REVIEW_MISMATCH = "review_mismatch"
    PLANNING_EVIDENCE_UNREVIEWED = "planning_evidence_unreviewed"
    PRICING_UNREVIEWED = "pricing_unreviewed"
    LIVE_VALIDATION_REQUIRED = "live_validation_required"
    ADMITTED = "admitted"


def _bound(snapshot: str, cap: int) -> ProviderAttemptBound:
    return ProviderAttemptBound(
        provider=OPENAI_PROVIDER,
        model=snapshot,
        data_recipient=OPENAI_DATA_RECIPIENT,
        max_attempts=1,
        max_output_tokens=cap,
        input_bounded=True,
    )


@dataclass(frozen=True)
class ReviewedProviderConfig:
    """The trusted inputs of an admitted provider. The key is never in ``repr``."""

    capabilities: ResponsesCapabilityProfile
    planning_evidence: ReviewedPlanningEvidence
    pricing: ProviderPricingTable
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.capabilities) is not ResponsesCapabilityProfile:
            raise TypeError("capabilities must be a reviewed ResponsesCapabilityProfile.")
        if type(self.planning_evidence) is not ReviewedPlanningEvidence:
            raise TypeError("planning_evidence must be ReviewedPlanningEvidence.")
        if type(self.pricing) is not ProviderPricingTable:
            raise TypeError("pricing must be a ProviderPricingTable.")
        if type(self.api_key) is not str or not self.api_key:
            raise ValueError("credential_missing")
        refusal = _reviewed_refusal(
            self.capabilities, self.planning_evidence, self.pricing, self.capabilities.review
        )
        if refusal is not None:
            raise ValueError(refusal.value)

    @property
    def snapshot(self) -> str:
        return self.capabilities.profile.model

    @property
    def planning_bound(self) -> ProviderAttemptBound:
        return _bound(self.snapshot, PLANNING_STAGE_OUTPUT_TOKENS)

    @property
    def execution_bound(self) -> ProviderAttemptBound:
        return _bound(self.snapshot, EXECUTION_CALL_OUTPUT_TOKENS)


def _reviewed_refusal(
    capabilities: ResponsesCapabilityProfile,
    evidence: ReviewedPlanningEvidence | None,
    pricing: ProviderPricingTable | None,
    review: str | None,
) -> AdmissionDecision | None:
    """Steps 7 to 9 for one reviewed profile; ``None`` when all pass."""
    snapshot = capabilities.profile.model
    if review is None or capabilities.review != review:
        return AdmissionDecision.REVIEW_MISMATCH
    if (
        evidence is None
        or evidence.evidence_version != review
        or evidence.covering(_bound(snapshot, PLANNING_STAGE_OUTPUT_TOKENS)) is None
    ):
        return AdmissionDecision.PLANNING_EVIDENCE_UNREVIEWED
    if pricing is None or pricing.profile_for(provider=OPENAI_PROVIDER, model=snapshot) is None:
        return AdmissionDecision.PRICING_UNREVIEWED
    return None


@dataclass(frozen=True)
class ProviderAdmission:
    decision: AdmissionDecision
    config: ReviewedProviderConfig | None = None


def admit_investigation_provider(
    settings: Settings,
    *,
    profiles: Mapping[str, ResponsesCapabilityProfile] = REVIEWED_CAPABILITY_PROFILES,
    planning_evidence: ReviewedPlanningEvidence | None = REVIEWED_PLANNING_EVIDENCE,
    pricing: ProviderPricingTable | None = REVIEWED_PRICING,
) -> ProviderAdmission:
    """Run the admission sequence (module docstring) without I/O; see ``AdmissionDecision``."""
    if settings.investigation_model_provider == "none":
        return ProviderAdmission(AdmissionDecision.PROVIDER_DISABLED)
    if settings.investigation_model_provider != OPENAI_RESPONSES:
        return ProviderAdmission(AdmissionDecision.PROVIDER_UNSUPPORTED)
    if settings.investigation_model_activation is not True:
        return ProviderAdmission(AdmissionDecision.ACTIVATION_OFF)
    if settings.local_only_mode:
        return ProviderAdmission(AdmissionDecision.LOCAL_ONLY)
    if not settings.openai_api_key:
        return ProviderAdmission(AdmissionDecision.CREDENTIAL_MISSING)
    snapshot = settings.investigation_model_snapshot
    if not snapshot:
        return ProviderAdmission(AdmissionDecision.SNAPSHOT_MISSING)
    capabilities = profiles.get(snapshot)
    # Exactly the reviewed type, for exactly this snapshot: no subclass, alias, or family match.
    if (
        not isinstance(capabilities, ResponsesCapabilityProfile)
        or type(capabilities) is not ResponsesCapabilityProfile
        or capabilities.profile.model != snapshot
    ):
        return ProviderAdmission(AdmissionDecision.CAPABILITY_UNREVIEWED)
    refusal = _reviewed_refusal(
        capabilities, planning_evidence, pricing, settings.investigation_model_review
    )
    if refusal is not None:
        return ProviderAdmission(refusal)
    if LIVE_ADMISSION_RELEASED is not True:
        return ProviderAdmission(AdmissionDecision.LIVE_VALIDATION_REQUIRED)
    assert planning_evidence is not None and pricing is not None
    config = ReviewedProviderConfig(
        capabilities=capabilities,
        planning_evidence=planning_evidence,
        pricing=pricing,
        api_key=settings.openai_api_key,
    )
    return ProviderAdmission(AdmissionDecision.ADMITTED, config)


class ConfiguredInvestigationAdapters:
    """A reviewed provider; one new adapter per ``planning()`` or ``execution()`` call.

    See "Ownership" in the module docstring. ``transport_factory`` is the test seam for a scripted
    ``httpx`` transport; ``None`` (production) means the real network transport, so construction
    here is reached only through a passed admission.
    """

    def __init__(
        self,
        config: ReviewedProviderConfig,
        *,
        transport_factory: Callable[[], httpx.BaseTransport | None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(config) is not ReviewedProviderConfig:
            raise TypeError("config must be a ReviewedProviderConfig.")
        self._config = config
        self._transport_factory = transport_factory
        self._clock = clock
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"ConfiguredInvestigationAdapters(model={self._config.snapshot!r})"

    def _adapter(self, cap: int) -> OpenAIResponsesBoundedAdapter:
        transport = ResponsesTransport(
            self._config.api_key, transport=self._transport_factory(), clock=self._clock
        )
        return OpenAIResponsesBoundedAdapter(
            self._config.capabilities,
            transport=transport,
            instructions=SYSTEM_INSTRUCTION,
            max_output_tokens=cap,
            clock=self._clock,
            sleep=self._sleep,
        )

    def planning(self) -> PlanningProvider:
        adapter = self._adapter(PLANNING_STAGE_OUTPUT_TOKENS)
        return PlanningProvider(
            adapter=adapter,
            evidence=self._config.planning_evidence,
            pricing=self._config.pricing,
            envelope=ProviderEnvelope(members=(adapter.bound,)),
        )

    def execution(self) -> ExecutionProvider:
        adapter = self._adapter(EXECUTION_CALL_OUTPUT_TOKENS)
        return ExecutionProvider(
            llm_client=adapter,
            envelope=ProviderEnvelope(members=(adapter.bound,)),
            max_input_tokens_per_call=MAX_EXECUTION_INPUT_TOKENS,
        )


@dataclass(frozen=True)
class AdapterSelection:
    adapters: InvestigationAdapters
    decision: AdmissionDecision


def select_investigation_adapters(settings: Settings) -> AdapterSelection:
    """``FailClosedAdapters`` unless admission passes; in S4 it never does."""
    admission = admit_investigation_provider(settings)
    if admission.decision is not AdmissionDecision.ADMITTED or admission.config is None:
        return AdapterSelection(FailClosedAdapters(), admission.decision)
    return AdapterSelection(ConfiguredInvestigationAdapters(admission.config), admission.decision)


__all__ = [
    "EXECUTION_CALL_OUTPUT_TOKENS",
    "LIVE_ADMISSION_RELEASED",
    "OPENAI_RESPONSES",
    "AdapterSelection",
    "AdmissionDecision",
    "ConfiguredInvestigationAdapters",
    "ProviderAdmission",
    "ReviewedProviderConfig",
    "admit_investigation_provider",
    "select_investigation_adapters",
]
