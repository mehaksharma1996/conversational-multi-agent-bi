"""Trusted investigation provider selection and adapter ownership (PROD-1 S4-S5c, #198).

``select_investigation_adapters(settings)`` is the one place server configuration can turn into an
investigation model provider. It returns ``FailClosedAdapters`` unless every step of the
admission sequence passes, in this order, and reports the first failing step as a content-free
``AdmissionDecision``:

1. ``INVESTIGATION_MODEL_PROVIDER`` names a supported provider (default ``none``);
2. ``INVESTIGATION_MODEL_ACTIVATION`` is on (default off);
3. ``LOCAL_ONLY_MODE`` is off (a hosted provider is withheld, as for every other client);
4. ``OPENAI_API_KEY`` is present (its absence never falls back to another provider);
5. ``INVESTIGATION_MODEL_SNAPSHOT`` names a pinned dated snapshot;
6. ``REVIEWED_PROVIDER_EVIDENCE`` holds a bundle for exactly that snapshot (PROD-1 S5c). There is
   no family or alias lookup, and the bundle's own snapshot must equal the key;
7. the bundle re-validates as the exact type (a forged or bypassed object fails), and
   ``evidence_refusal`` passes: one schema version, one identity, one review ID, a current review,
   the exact planning and execution bounds, and a price review covering every billable component
   (``apps.api.provider_evidence``);
8. ``INVESTIGATION_MODEL_REVIEW`` equals the bundle's review ID;
9. the release gate ``LIVE_ADMISSION_RELEASED`` is open. It is a code constant, not a setting:
   no release has passed live validation (S6) or activation review (DEP-1), so it is ``False``.

Nothing is admitted today: ``REVIEWED_PROVIDER_EVIDENCE`` is empty and the gate is closed; either
blocks admission on its own. Budget, consent, authorization and approvals, and the operation
deadline are not admission-time facts: the planning and tool gateways check them before every
model call, unchanged.

Selection does no I/O and builds no adapter or transport. Nothing a request carries reaches it:
the provider, snapshot, endpoint (fixed in ``src.llm.responses_transport``), and key come only
from the server environment.

**Ownership.** ``ConfiguredInvestigationAdapters`` builds a new ``OpenAIResponsesBoundedAdapter``
and ``ResponsesTransport`` on every ``planning()`` and ``execution()`` call and keeps no reference
to them. ``InvestigationRuntime`` makes each call once per investigation (``planning()`` in the
plan job, ``execution()`` at plan approval), and the execute job and every resume use that one
provider, so one adapter instance serves every model call of that stage: an uncertain create halts
it for the rest of the investigation, and no gateway, task retry, or resume obtains a fresh one.
Distinct investigations, and so distinct tenants, never share an instance. This guarantee relies
on the single-process runtime: a restart makes every live investigation ``interrupted`` and never
rebuilds a run. It does not prevent duplicates across processes or hosts.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

import httpx

from apps.api.investigations import (
    MAX_EXECUTION_INPUT_TOKENS,
    ExecutionProvider,
    FailClosedAdapters,
    InvestigationAdapters,
    PlanningProvider,
)
from apps.api.provider_evidence import (
    EXECUTION_CALL_OUTPUT_TOKENS,
    ReviewedProviderEvidence,
    evidence_refusal,
    revalidated,
)
from config.settings import Settings
from packages.investigation.budget import ProviderPricingTable
from packages.investigation.gateway import (
    PLANNING_STAGE_OUTPUT_TOKENS,
    ProviderAttemptBound,
    ProviderEnvelope,
    ReviewedExecutionEvidence,
    ReviewedPlanningEvidence,
)
from src.llm.openai_responses_bounded import (
    OpenAIResponsesBoundedAdapter,
    ResponsesCapabilityProfile,
)
from src.llm.providers import SYSTEM_INSTRUCTION
from src.llm.responses_transport import ResponsesTransport

OPENAI_RESPONSES = "openai_responses"

# Admission step 9. Only a reviewed code change, after S6 and DEP-1, may open it.
LIVE_ADMISSION_RELEASED: Final = False

# Reviewed evidence bundles by exact snapshot. None exist: a bundle needs reviewed provider
# evidence (S6, under a separate spend approval), never a synthetic record.
REVIEWED_PROVIDER_EVIDENCE: Mapping[str, ReviewedProviderEvidence] = MappingProxyType({})


class AdmissionDecision(StrEnum):
    """Content-free result of provider selection: the first failing step, or ``admitted``."""

    PROVIDER_DISABLED = "provider_disabled"
    PROVIDER_UNSUPPORTED = "provider_unsupported"
    ACTIVATION_OFF = "activation_off"
    LOCAL_ONLY = "local_only_mode"
    CREDENTIAL_MISSING = "credential_missing"
    SNAPSHOT_MISSING = "snapshot_missing"
    CAPABILITY_UNREVIEWED = "capability_unreviewed"  # no bundle for this exact snapshot
    EVIDENCE_INVALID = "evidence_invalid"
    EVIDENCE_SCHEMA_UNSUPPORTED = "evidence_schema_unsupported"
    EVIDENCE_IDENTITY_MISMATCH = "evidence_identity_mismatch"
    EVIDENCE_EXPIRED = "evidence_expired"
    REVIEW_MISMATCH = "review_mismatch"
    PLANNING_EVIDENCE_UNREVIEWED = "planning_evidence_unreviewed"
    EXECUTION_EVIDENCE_UNREVIEWED = "execution_evidence_unreviewed"
    PRICING_UNREVIEWED = "pricing_unreviewed"
    PRICING_UNBOUNDED = "pricing_unbounded"
    LIVE_VALIDATION_REQUIRED = "live_validation_required"
    ADMITTED = "admitted"


def _today() -> date:
    return datetime.now(UTC).date()


@dataclass(frozen=True)
class ReviewedProviderConfig:
    """The trusted inputs of an admitted provider. The key is never in ``repr``."""

    evidence: ReviewedProviderEvidence
    api_key: str = field(repr=False)
    today: date = field(default_factory=_today)

    def __post_init__(self) -> None:
        checked = revalidated(self.evidence)
        if checked is None:
            raise TypeError("evidence must be a valid ReviewedProviderEvidence.")
        if type(self.api_key) is not str or not self.api_key:
            raise ValueError("credential_missing")
        refusal = evidence_refusal(checked, self.today)
        if refusal is not None:
            raise ValueError(refusal.value)
        object.__setattr__(self, "evidence", checked)

    @property
    def capabilities(self) -> ResponsesCapabilityProfile:
        return self.evidence.capabilities

    @property
    def planning_evidence(self) -> ReviewedPlanningEvidence:
        return self.evidence.planning_evidence

    @property
    def execution_evidence(self) -> ReviewedExecutionEvidence:
        return self.evidence.execution_evidence

    @property
    def pricing(self) -> ProviderPricingTable:
        return self.evidence.pricing

    @property
    def snapshot(self) -> str:
        return self.evidence.model

    @property
    def planning_bound(self) -> ProviderAttemptBound:
        return self.evidence.bound(PLANNING_STAGE_OUTPUT_TOKENS)

    @property
    def execution_bound(self) -> ProviderAttemptBound:
        return self.evidence.bound(EXECUTION_CALL_OUTPUT_TOKENS)


@dataclass(frozen=True)
class ProviderAdmission:
    decision: AdmissionDecision
    config: ReviewedProviderConfig | None = None


def admit_investigation_provider(
    settings: Settings,
    *,
    evidence: Mapping[str, ReviewedProviderEvidence] = REVIEWED_PROVIDER_EVIDENCE,
    today: date | None = None,
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
    entry = evidence.get(snapshot)
    if entry is None:
        return ProviderAdmission(AdmissionDecision.CAPABILITY_UNREVIEWED)
    checked = revalidated(entry)
    if checked is None:
        return ProviderAdmission(AdmissionDecision.EVIDENCE_INVALID)
    if checked.model != snapshot:  # filed under an alias or another snapshot's key
        return ProviderAdmission(AdmissionDecision.EVIDENCE_IDENTITY_MISMATCH)
    refusal = evidence_refusal(checked, today or _today())
    if refusal is not None:
        return ProviderAdmission(AdmissionDecision(refusal.value))
    if settings.investigation_model_review != checked.review_id:
        return ProviderAdmission(AdmissionDecision.REVIEW_MISMATCH)
    if LIVE_ADMISSION_RELEASED is not True:
        return ProviderAdmission(AdmissionDecision.LIVE_VALIDATION_REQUIRED)
    config = ReviewedProviderConfig(
        evidence=checked, api_key=settings.openai_api_key, today=today or _today()
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
            evidence=self._config.execution_evidence,
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
