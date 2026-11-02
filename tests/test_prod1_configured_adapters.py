"""PROD-1 S4: trusted provider configuration, adapter ownership, and versioned OpenAI consent.

Everything runs offline. Provider traffic goes only to ``httpx.MockTransport`` scripts, the
placeholder key cannot authenticate anywhere, and any non-loopback connection fails the test
(``_no_network``). Nothing here proves live provider behaviour.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api import investigation_providers as providers
from apps.api.consent_notices import (
    BASE_NOTICE_VERSION,
    OPENAI_NOTICE,
    OPENAI_NOTICE_VERSION,
    disclosed_recipients,
    discloses,
)
from apps.api.errors import ResourceNotFoundError
from apps.api.investigation_providers import (
    EXECUTION_CALL_OUTPUT_TOKENS,
    AdmissionDecision,
    ConfiguredInvestigationAdapters,
    ReviewedProviderConfig,
    admit_investigation_provider,
    select_investigation_adapters,
)
from apps.api.investigations import FailClosedAdapters, _consent
from apps.api.main import create_app
from apps.api.provider_evidence import ReviewedPriceEvidence, ReviewedProviderEvidence
from config.settings import Settings, get_settings
from packages.governance import InMemoryAuditSink
from packages.investigation.budget import ProviderCostProfile, ProviderPricingTable
from packages.investigation.gateway import (
    PLANNING_STAGE_OUTPUT_TOKENS,
    ReviewedExecutionCapability,
    ReviewedExecutionEvidence,
    ReviewedPlanningCapability,
    ReviewedPlanningEvidence,
)
from packages.investigation.refusals import RefusalCode
from packages.observability import InMemoryTelemetrySink
from src.llm.openai_responses_bounded import OpenAIResponsesBoundedAdapter
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_api_investigations import _as, _create, _proposal, _status, _until
from tests.test_openai_responses_bounded import (
    CANCEL,
    COUNT,
    CREATE,
    FAKE_KEY,
    SNAPSHOT,
    Clock,
    Provider,
    after_create,
    cancelled,
    capabilities,
    counted,
    queued,
)
from tests.test_prod1_gateway_integration import (
    EXEC_BOUND,
    PLAN_BOUND,
    _no_network,  # noqa: F401 - autouse: every test here refuses real network access
    exec_harness,
    planning_run,
    refused,
    research,
    retryable_plan,
)
from tests.test_utils import isolated_directory_path

REVIEW = "test-fixture-1"  # the review id of ``capabilities()``
OTHER_TENANT = "b" * 32


def reviewed_evidence(version: str = REVIEW) -> ReviewedPlanningEvidence:
    return ReviewedPlanningEvidence(
        evidence_version=version,
        capabilities=(
            ReviewedPlanningCapability(
                bound=PLAN_BOUND,
                evidence_version=version,
                provider_deadline_enforced=True,
                termination_confirmed=True,
            ),
        ),
    )


def reviewed_pricing(model: str = SNAPSHOT, version: str = REVIEW) -> ProviderPricingTable:
    return ProviderPricingTable(
        version=version,
        currency="USD",
        profiles=(
            ProviderCostProfile(
                provider="openai",
                model=model,
                input_per_million_usd=0.4,
                output_per_million_usd=1.6,
                output_cap_enforced=True,
                input_bounded_before_call=True,
                retries_metered=True,
            ),
        ),
    )


def configured(root: Path, **changes: Any) -> Settings:
    """Settings that pass every configuration step: only review records and the gate remain."""
    values: dict[str, Any] = {
        "investigation_model_provider": "openai_responses",
        "investigation_model_activation": True,
        "openai_api_key": FAKE_KEY,
        "investigation_model_snapshot": SNAPSHOT,
        "investigation_model_review": REVIEW,
    }
    values.update(changes)
    return replace(_settings(root), **values)


def reviewed_execution(version: str = REVIEW, bound: Any = None) -> ReviewedExecutionEvidence:
    """A SYNTHETIC execution review record (tests only; production registries stay empty)."""
    return ReviewedExecutionEvidence(
        evidence_version=version,
        capabilities=(
            ReviewedExecutionCapability(
                bound=EXEC_BOUND if bound is None else bound,
                evidence_version=version,
                provider_deadline_enforced=True,
                termination_confirmed=True,
                usage_reported=True,
            ),
        ),
    )


def reviewed_price(**changes: Any) -> ReviewedPriceEvidence:
    """A SYNTHETIC price review (tests only): not a real or third-party OpenAI price."""
    values: dict[str, Any] = {
        "provider": "openai",
        "model": SNAPSHOT,
        "review_id": REVIEW,
        "currency": "USD",
        "input_per_million_usd": 0.4,
        "output_per_million_usd": 1.6,
        "billable_components": ("input_tokens", "output_tokens"),
        "count_charge": "not_billed",
    }
    values.update(changes)
    return ReviewedPriceEvidence(**values)


def bundle(**changes: Any) -> ReviewedProviderEvidence:
    """A SYNTHETIC, internally consistent evidence bundle (PROD-1 S5c), current today."""
    today = datetime.now(UTC).date()
    values: dict[str, Any] = {
        "schema_version": 1,
        "review_id": REVIEW,
        "provider": "openai",
        "model": SNAPSHOT,
        "data_recipient": "OpenAI",
        "source": "synthetic-test-fixture",
        "reviewed_on": today - timedelta(days=1),
        "expires_on": today + timedelta(days=30),
        "capabilities": capabilities(),
        "planning_evidence": reviewed_evidence(),
        "execution_evidence": reviewed_execution(),
        "pricing": reviewed_pricing(),
        "price": reviewed_price(),
    }
    values.update(changes)
    return ReviewedProviderEvidence(**values)


def reviewed() -> dict[str, Any]:
    return {"evidence": {SNAPSHOT: bundle()}}


def config() -> ReviewedProviderConfig:
    return ReviewedProviderConfig(evidence=bundle(), api_key=FAKE_KEY)


def scripted(provider: Provider) -> ConfiguredInvestigationAdapters:
    clock = provider.clock
    return ConfiguredInvestigationAdapters(
        config(),
        transport_factory=lambda: httpx.MockTransport(provider),
        clock=clock,
        sleep=clock.sleep,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


# --- 1-8, 22: the admission sequence --------------------------------------------------------


def test_the_default_is_fail_closed_and_names_no_recipient(root: Path) -> None:
    settings = _settings(root)
    assert settings.investigation_model_provider == "none"
    assert settings.investigation_model_activation is False
    selection = select_investigation_adapters(settings)
    assert selection.decision is AdmissionDecision.PROVIDER_DISABLED
    assert type(selection.adapters) is FailClosedAdapters
    assert selection.adapters.planning().adapter is None
    assert selection.adapters.execution().llm_client is None
    assert settings.investigation_recipients() == ()
    assert settings.consent_recipients() == settings.hosted_recipients()


def test_the_environment_defaults_are_off_and_need_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "INVESTIGATION_MODEL_PROVIDER",
        "INVESTIGATION_MODEL_ACTIVATION",
        "INVESTIGATION_MODEL_SNAPSHOT",
        "INVESTIGATION_MODEL_REVIEW",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = get_settings()
    assert settings.investigation_model_provider == "none"
    assert settings.openai_api_key is None
    assert select_investigation_adapters(settings).decision is AdmissionDecision.PROVIDER_DISABLED


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("INVESTIGATION_MODEL_PROVIDER", "azure-openai-secret-host"),
        ("INVESTIGATION_MODEL_SNAPSHOT", "gpt-4.1-mini"),  # a family alias, not a dated snapshot
        ("INVESTIGATION_MODEL_REVIEW", "Review With Spaces"),
    ],
)
def test_malformed_settings_fail_startup_without_echoing_the_value(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as caught:
        get_settings()
    assert value not in str(caught.value)


def test_create_app_rejects_malformed_injected_settings(root: Path) -> None:
    with pytest.raises(ValueError):
        create_app(settings=configured(root, investigation_model_provider="gemini"))


@pytest.mark.parametrize(
    ("changes", "decision"),
    [
        ({"investigation_model_provider": "none"}, AdmissionDecision.PROVIDER_DISABLED),
        ({"investigation_model_provider": "anthropic"}, AdmissionDecision.PROVIDER_UNSUPPORTED),
        ({"investigation_model_activation": False}, AdmissionDecision.ACTIVATION_OFF),
        ({"local_only_mode": True}, AdmissionDecision.LOCAL_ONLY),
        ({"openai_api_key": None}, AdmissionDecision.CREDENTIAL_MISSING),
        ({"openai_api_key": ""}, AdmissionDecision.CREDENTIAL_MISSING),
        ({"investigation_model_snapshot": None}, AdmissionDecision.SNAPSHOT_MISSING),
        (
            {"investigation_model_snapshot": "test-model-2099-02-02"},
            AdmissionDecision.CAPABILITY_UNREVIEWED,
        ),
        ({"investigation_model_review": None}, AdmissionDecision.REVIEW_MISMATCH),
        ({"investigation_model_review": "other-review-2"}, AdmissionDecision.REVIEW_MISMATCH),
    ],
)
def test_each_configuration_step_is_required(
    root: Path, changes: dict[str, Any], decision: AdmissionDecision
) -> None:
    admission = admit_investigation_provider(configured(root, **changes), **reviewed())
    assert admission.decision is decision and admission.config is None


@pytest.mark.parametrize(
    ("registry", "decision"),
    [
        ({}, AdmissionDecision.CAPABILITY_UNREVIEWED),
        # a bundle filed under an alias never serves another snapshot
        ({"test-model": bundle()}, AdmissionDecision.CAPABILITY_UNREVIEWED),
        (
            {SNAPSHOT: bundle(execution_evidence=reviewed_execution(bound=PLAN_BOUND))},
            AdmissionDecision.EXECUTION_EVIDENCE_UNREVIEWED,
        ),
    ],
)
def test_each_review_record_is_required(
    root: Path, registry: dict[str, Any], decision: AdmissionDecision
) -> None:
    """The full evidence matrix is in ``tests/test_prod1_s5c_evidence_registry.py``."""
    admission = admit_investigation_provider(configured(root), evidence=registry)
    assert admission.decision is decision and admission.config is None


def test_production_records_are_empty_so_valid_settings_admit_nothing(root: Path) -> None:
    selection = select_investigation_adapters(configured(root))
    assert selection.decision is AdmissionDecision.CAPABILITY_UNREVIEWED
    assert type(selection.adapters) is FailClosedAdapters
    assert dict(providers.REVIEWED_PROVIDER_EVIDENCE) == {}


def test_the_release_gate_blocks_even_a_fully_reviewed_configuration(root: Path) -> None:
    assert providers.LIVE_ADMISSION_RELEASED is False
    admission = admit_investigation_provider(configured(root), **reviewed())
    assert admission.decision is AdmissionDecision.LIVE_VALIDATION_REQUIRED
    assert admission.config is None


def test_the_sequence_is_complete_once_the_gate_opens(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only to show nothing else is missing: production cannot open this gate."""
    monkeypatch.setattr(providers, "LIVE_ADMISSION_RELEASED", True)
    admission = admit_investigation_provider(configured(root), **reviewed())
    assert admission.decision is AdmissionDecision.ADMITTED and admission.config is not None
    assert admission.config.snapshot == SNAPSHOT
    # ...and activation off still refuses, even with the gate open.
    off = admit_investigation_provider(
        configured(root, investigation_model_activation=False), **reviewed()
    )
    assert off.decision is AdmissionDecision.ACTIVATION_OFF


def test_the_default_app_fails_closed_even_with_valid_looking_configuration(root: Path) -> None:
    app = create_app(
        settings=configured(root),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=InMemoryTelemetrySink(),
        audit_sink=InMemoryAuditSink(),
    )
    assert app.state.investigation_provider_decision is AdmissionDecision.CAPABILITY_UNREVIEWED
    assert type(app.state.investigations._adapters) is FailClosedAdapters
    plain = create_app(
        settings=_settings(root / "plain"),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=InMemoryTelemetrySink(),
    )
    assert plain.state.investigation_provider_decision is AdmissionDecision.PROVIDER_DISABLED


def test_the_reviewed_config_refuses_inconsistent_records_and_hides_the_key() -> None:
    with pytest.raises(ValueError, match="credential_missing"):
        replace(config(), api_key="")
    with pytest.raises(ValueError, match="review_mismatch"):
        replace(config(), evidence=bundle(planning_evidence=reviewed_evidence("other-review-2")))
    with pytest.raises(ValueError, match="pricing_unreviewed"):
        replace(config(), evidence=bundle(pricing=reviewed_pricing("test-model-2099-02-02")))
    with pytest.raises(TypeError):
        ConfiguredInvestigationAdapters(object())  # type: ignore[arg-type]
    rendered = repr(config()) + repr(scripted(Provider(Clock())))
    assert FAKE_KEY not in rendered


def test_selection_and_construction_send_nothing(root: Path) -> None:
    provider = Provider(Clock())
    adapters = scripted(provider)
    select_investigation_adapters(configured(root))
    adapters.planning()
    adapters.execution()
    assert provider.calls == []


def test_the_browser_cannot_select_a_provider_model_or_recipient(root: Path) -> None:
    app = create_app(
        settings=configured(root, investigation_routing_enabled=True),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=InMemoryTelemetrySink(),
    )
    with TestClient(app) as client:
        workspace_id, conversation_id, message_id = _proposal(client)
        for extra in (
            {"provider": "openai_responses"},
            {"model": "gpt-5-2099-01-01"},
            {"base_url": "https://attacker.example"},
        ):
            created = client.post(
                f"/api/v1/conversations/{conversation_id}/investigations",
                json={"message_id": message_id, **extra},
                headers={"Idempotency-Key": f"k-{next(iter(extra))}"},
            )
            assert created.status_code == 422
        consent = client.put(
            f"/api/v1/workspaces/{workspace_id}/consent",
            json={"accepted": True, "notice_version": "2026-09", "recipients": ["OpenAI"]},
        )
        assert consent.status_code == 422
        assert type(app.state.investigations._adapters) is FailClosedAdapters


# --- 9-15: versioned consent ---------------------------------------------------------------


def test_only_the_openai_notice_discloses_openai() -> None:
    assert OPENAI_NOTICE.version == OPENAI_NOTICE_VERSION != BASE_NOTICE_VERSION
    assert discloses(OPENAI_NOTICE_VERSION, "OpenAI")
    for version in (BASE_NOTICE_VERSION, None, "", "2026-10-openai-0", "2026-10-OPENAI-1"):
        assert not discloses(version, "OpenAI")
    for version in (BASE_NOTICE_VERSION, OPENAI_NOTICE_VERSION, None):
        assert discloses(version, "Gemini")  # existing recipients are unchanged
    assert disclosed_recipients(BASE_NOTICE_VERSION, ("Gemini", "OpenAI")) == ("Gemini",)


def test_the_notice_text_discloses_recipient_data_and_retention_limits() -> None:
    text = OPENAI_NOTICE.text
    for phrase in (
        "OpenAI",
        "external processor",
        "query results",
        "uploaded documents",
        "store=false",
        "about ten minutes",
        "not a guaranteed deletion deadline",
        "does not turn on",
    ):
        assert phrase in text


class ConsentApp:
    def __init__(self, root: Path, **changes: Any) -> None:
        self.root = root
        self.settings = replace(configured(root, **changes), durable_metadata=True)
        self.audit = InMemoryAuditSink()

    def app(self) -> Any:
        return create_app(
            settings=self.settings,
            embedder_factory=lambda _model: FakeEmbedder(),
            llm_client_factory=lambda _settings: FakeLLM(),
            telemetry_sink=InMemoryTelemetrySink(),
            audit_sink=self.audit,
        )


def _accept(client: TestClient, workspace_id: str, version: str) -> Any:
    return client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": version},
    )


def _snapshot(client: TestClient, workspace_id: str) -> Any:
    repository = client.app.state.repository
    return _consent(repository._workspaces[workspace_id])


def test_a_new_workspace_without_consent_does_not_satisfy_openai(root: Path) -> None:
    with TestClient(ConsentApp(root).app()) as client:
        workspace_id = client.post("/api/v1/workspaces").json()["id"]
        assert not _snapshot(client, workspace_id).satisfies({"OpenAI"})


def test_consent_to_the_older_notice_never_grants_openai(root: Path) -> None:
    with TestClient(ConsentApp(root).app()) as client:
        workspace_id = client.post("/api/v1/workspaces").json()["id"]
        body = _accept(client, workspace_id, BASE_NOTICE_VERSION).json()
        assert body["data_recipients"] == ["Gemini"]
        snapshot = _snapshot(client, workspace_id)
        assert snapshot.satisfies({"Gemini"}) and not snapshot.satisfies({"OpenAI"})


def test_explicit_acceptance_of_the_openai_notice_grants_it_with_content_free_audit(
    root: Path,
) -> None:
    deployment = ConsentApp(root)
    with TestClient(deployment.app()) as client:
        workspace_id = client.post("/api/v1/workspaces").json()["id"]
        body = _accept(client, workspace_id, OPENAI_NOTICE_VERSION).json()
        assert body["data_recipients"] == ["Gemini", "OpenAI"]
        assert body["notice_version"] == OPENAI_NOTICE_VERSION
        assert _snapshot(client, workspace_id).satisfies({"Gemini", "OpenAI"})
    event = next(e for e in deployment.audit.events if e.name == "consent.accepted")
    rendered = json.dumps(event.attributes, default=str)
    assert OPENAI_NOTICE_VERSION in rendered
    for leaked in (OPENAI_NOTICE.text[:40], FAKE_KEY, SNAPSHOT):
        assert leaked not in rendered


def test_openai_consent_needs_an_openai_provider_to_be_configured(root: Path) -> None:
    with TestClient(ConsentApp(root, investigation_model_activation=False).app()) as client:
        workspace_id = client.post("/api/v1/workspaces").json()["id"]
        body = _accept(client, workspace_id, OPENAI_NOTICE_VERSION).json()
        assert body["data_recipients"] == ["Gemini"]
        assert not _snapshot(client, workspace_id).satisfies({"OpenAI"})


def test_accepting_an_older_notice_again_withdraws_openai(root: Path) -> None:
    """A later acceptance replaces the whole consent record (S5d adds an explicit withdrawal)."""
    with TestClient(ConsentApp(root).app()) as client:
        workspace_id = client.post("/api/v1/workspaces").json()["id"]
        _accept(client, workspace_id, OPENAI_NOTICE_VERSION)
        assert _snapshot(client, workspace_id).satisfies({"OpenAI"})
        assert _accept(client, workspace_id, BASE_NOTICE_VERSION).json()["data_recipients"] == [
            "Gemini"
        ]
        assert not _snapshot(client, workspace_id).satisfies({"OpenAI"})
        repository = client.app.state.repository
        record = repository._workspaces[workspace_id]
        cleared = replace(record, consent_accepted_at=None)  # a revoked (cleared) record
        assert not _consent(cleared).satisfies({"OpenAI"})


def test_a_stale_or_edited_record_naming_openai_is_not_trusted(root: Path) -> None:
    with TestClient(ConsentApp(root).app()) as client:
        workspace_id = client.post("/api/v1/workspaces").json()["id"]
        repository = client.app.state.repository
        record = repository._workspaces[workspace_id]
        # S5d: a version the server does not recognise now grants nobody, not even Gemini.
        for version, granted in (
            (BASE_NOTICE_VERSION, ("Gemini",)),
            (None, ()),
            ("2026-10-openai-0", ()),
        ):
            stale = replace(
                record,
                consent_accepted_at=datetime.now(UTC),
                consent_notice_version=version,
                consent_recipients=("Gemini", "OpenAI"),
            )
            snapshot = _consent(stale)
            assert snapshot.recipients == granted
            assert not snapshot.satisfies({"OpenAI"})


def test_consent_never_crosses_tenants(root: Path) -> None:
    app = ConsentApp(root).app()
    with TestClient(app) as client:
        workspace_id = client.post("/api/v1/workspaces").json()["id"]
        _accept(client, workspace_id, OPENAI_NOTICE_VERSION)
        _as(app, OTHER_TENANT)
        assert _accept(client, workspace_id, OPENAI_NOTICE_VERSION).status_code == 404
        other = client.post("/api/v1/workspaces").json()["id"]
        assert not _snapshot(client, other).satisfies({"OpenAI"})
        repository = app.state.repository
        with pytest.raises(ResourceNotFoundError):
            repository.get_workspace(workspace_id, OTHER_TENANT)
        app.dependency_overrides.clear()


def test_restart_keeps_older_consent_older_and_newer_consent_current(root: Path) -> None:
    deployment = ConsentApp(root)
    with TestClient(deployment.app()) as client:
        old_id = client.post("/api/v1/workspaces").json()["id"]
        new_id = client.post("/api/v1/workspaces").json()["id"]
        repository = client.app.state.repository
        tenant = repository._workspaces[old_id].tenant_id
        # A pre-S4 row that somehow names OpenAI under the older notice (persisted as-is).
        repository.set_consent(old_id, tenant, BASE_NOTICE_VERSION, recipients=("Gemini", "OpenAI"))
        _accept(client, new_id, OPENAI_NOTICE_VERSION)
    with TestClient(deployment.app()) as client:  # a restart reads the durable metadata
        assert not _snapshot(client, old_id).satisfies({"OpenAI"})
        assert _snapshot(client, new_id).satisfies({"OpenAI"})


# --- 16-21: adapter ownership --------------------------------------------------------------


def test_each_call_builds_a_fresh_unshared_adapter_and_transport() -> None:
    built: list[httpx.MockTransport] = []
    clock = Clock()

    def factory() -> httpx.MockTransport:
        transport = httpx.MockTransport(Provider(clock))
        built.append(transport)
        return transport

    adapters = ConfiguredInvestigationAdapters(
        config(), transport_factory=factory, clock=clock, sleep=clock.sleep
    )
    plans = [adapters.planning() for _ in range(2)]
    runs = [adapters.execution() for _ in range(2)]
    instances = [plan.adapter for plan in plans] + [run.llm_client for run in runs]
    assert len({id(instance) for instance in instances}) == 4 and len(built) == 4
    assert all(type(instance) is OpenAIResponsesBoundedAdapter for instance in instances)
    assert [plan.envelope.members for plan in plans] == [(PLAN_BOUND,)] * 2
    assert [run.envelope.members for run in runs] == [(EXEC_BOUND,)] * 2
    assert EXEC_BOUND.max_output_tokens == EXECUTION_CALL_OUTPUT_TOKENS
    assert plans[0].envelope.data_recipients == frozenset({"OpenAI"})
    assert not hasattr(adapters, "_cache") and len(vars(adapters)) == 4  # no instance retained


def test_an_uncertain_planning_create_halts_the_owned_adapter_across_gateways() -> None:
    provider = Provider(Clock(), counted(1_500), httpx.ReadTimeout("create timed out"))
    owned = scripted(provider).planning().adapter
    assert isinstance(owned, OpenAIResponsesBoundedAdapter)
    first = planning_run(provider=provider, model_adapter=owned)
    first.initial()
    assert owned.halted and provider.paths == [COUNT, CREATE]
    replacement = planning_run(provider=provider, model_adapter=owned)  # a new gateway
    refused(replacement.initial(), RefusalCode.METERING_UNBOUNDED)
    assert provider.paths == [COUNT, CREATE]


def test_an_uncertain_execution_create_survives_retry_and_gateway_replacement() -> None:
    provider = Provider(Clock(), counted(400), httpx.ReadTimeout("create timed out"))
    owned = scripted(provider).execution().llm_client
    h, _, _ = exec_harness(provider=provider, llm=owned, plan=retryable_plan())
    research(h)
    assert owned.halted  # type: ignore[union-attr]
    retry = h.begin()
    assert retry.attempt is not None
    assert h.research(retry.attempt).refusal is RefusalCode.METERING_UNBOUNDED
    replacement, _, _ = exec_harness(provider=provider, llm=owned)
    assert research(replacement).refusal is RefusalCode.METERING_UNBOUNDED
    assert provider.paths == [COUNT, CREATE]  # one create for the whole logical operation


def test_cancellation_targets_only_the_owned_response() -> None:
    provider = Provider(Clock(), counted(400), queued())
    provider.steps.append(cancelled({"input_tokens": 400, "output_tokens": 9}))
    owned = scripted(provider).execution().llm_client
    h, _, _ = exec_harness(provider=provider, llm=owned)
    started = h.begin()
    h.gateway._cancellation = after_create(provider)
    assert h.research(started.attempt).refusal is RefusalCode.CANCELLED
    assert provider.paths == [COUNT, CREATE, CANCEL]
    assert provider.hosts == {"https://api.openai.com"}


class RecordingAdapters(ConfiguredInvestigationAdapters):
    def __init__(self, provider: Any) -> None:
        super().__init__(config(), transport_factory=lambda: httpx.MockTransport(provider))
        self.planning_adapters: list[object] = []
        self.execution_calls = 0

    def planning(self) -> Any:
        built = super().planning()
        self.planning_adapters.append(built.adapter)
        return built

    def execution(self) -> Any:
        self.execution_calls += 1
        return super().execution()


class CountRefused:
    """Every count is a provider error, so planning fails closed after one request."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        return httpx.Response(500, json={"error": {"message": "offline script"}})


def _grant_openai(client: TestClient, workspace_id: str, version: str) -> None:
    repository = client.app.state.repository
    record = repository._workspaces[workspace_id]
    repository._workspaces[workspace_id] = replace(
        record,
        consent_accepted_at=datetime.now(UTC),
        consent_notice_version=version,
        consent_recipients=(*record.consent_recipients, "OpenAI"),
    )


def _deployment(root: Path, adapters: Any) -> Any:
    settings = replace(
        _settings(root),
        durable_metadata=True,
        investigation_routing_enabled=True,
    )
    return create_app(
        settings=settings,
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=InMemoryTelemetrySink(),
        audit_sink=InMemoryAuditSink(),
        investigation_adapters=adapters,
    )


def test_the_runtime_needs_current_consent_before_the_real_adapter_sends_anything() -> None:
    script = CountRefused()
    adapters = RecordingAdapters(script)
    with TestClient(_deployment(isolated_directory_path("prod1_s4_consent"), adapters)) as client:
        workspace_id, conversation_id, message_id = _proposal(client)
        _grant_openai(client, workspace_id, BASE_NOTICE_VERSION)  # stale: names OpenAI, old notice
        investigation_id = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation_id, _status("failed", "awaiting_plan_approval"))
        assert body["status"] == "failed"
        assert body["explanation_code"] == "consent_missing"
        assert script.paths == []  # refused before any provider request


def test_the_runtime_owns_one_planning_adapter_per_investigation() -> None:
    script = CountRefused()
    adapters = RecordingAdapters(script)
    root = isolated_directory_path("prod1_s4_owned")
    with TestClient(_deployment(root, adapters)) as client:
        ids = []
        for key in ("a", "b"):
            workspace_id, conversation_id, message_id = _proposal(client)
            _grant_openai(client, workspace_id, OPENAI_NOTICE_VERSION)
            investigation_id = _create(client, conversation_id, message_id, key=key).json()["id"]
            body = _until(client, investigation_id, _status("failed", "awaiting_plan_approval"))
            assert body["status"] == "failed"
            assert body["explanation_code"] == "metering_unbounded"
            ids.append(investigation_id)
        assert len(adapters.planning_adapters) == 2  # one per investigation, never rebuilt
        assert adapters.planning_adapters[0] is not adapters.planning_adapters[1]
        assert adapters.execution_calls == 0
        assert script.paths == ["/v1/responses/input_tokens"] * 2  # one count each, no create
    with TestClient(_deployment(root, adapters)) as client:  # restart
        for investigation_id in ids:
            status = client.get(f"/api/v1/investigations/{investigation_id}").json()["status"]
            assert status == "failed"
        assert (
            len(adapters.planning_adapters) == 2
            and script.paths == ["/v1/responses/input_tokens"] * 2
        )


# --- 23-25: no network, no credential, no leakage --------------------------------------------


def test_logs_and_errors_carry_no_key_prompt_or_response_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    provider = Provider(Clock(), counted(400), httpx.ReadTimeout("create timed out"))
    owned = scripted(provider).execution().llm_client
    h, _, _ = exec_harness(provider=provider, llm=owned)
    research(h)
    rendered = caplog.text + repr(h.state) + repr(owned) + repr(scripted(provider))
    for secret in (FAKE_KEY, "Prices rose by 5%", "resp_0123abcd"):
        assert secret not in rendered


def test_settings_repr_hides_the_openai_key(root: Path) -> None:
    assert FAKE_KEY not in repr(configured(root))


def test_planning_stage_output_cap_is_unchanged() -> None:
    assert PLANNING_STAGE_OUTPUT_TOKENS == 2_000
    assert PLAN_BOUND.max_output_tokens == PLANNING_STAGE_OUTPUT_TOKENS
