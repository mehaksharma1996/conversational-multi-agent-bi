"""PROD-1 S5b: strict reviewed execution admission and consent at plan approval.

Every reviewed record here is SYNTHETIC and test-only; production registries stay empty and no
provider is admitted. Provider traffic goes only to scripted ``httpx.MockTransport`` handlers,
and any non-loopback connection fails the test. Nothing here proves live provider behaviour.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api import investigation_providers as providers
from packages.investigation.gateway import (
    MeteringUnbounded,
    ProviderEnvelope,
    ReviewedExecutionCapability,
    ReviewedExecutionEvidence,
)
from packages.investigation.refusals import RefusalCode
from src.llm.bounded import admit_execution_adapter
from src.orchestration import tool_gateway
from src.orchestration.tool_gateway import (
    SCRIPTED_EXECUTION_ADMISSION,
    InvestigationToolGateway,
    ScriptedExecutionAdmission,
    admitted_model_adapter,
)
from tests.test_api_investigations import (
    Deployment,
    _approve,
    _approve_wave,
    _create,
    _proposal,
)
from tests.test_investigation_tool_gateway import EXACT_BOUND, EXACT_ENVELOPE, ExactAdapter
from tests.test_investigation_tool_gateway import _harness as tool_harness
from tests.test_openai_responses_bounded import FAKE_KEY, Clock, Provider, counted
from tests.test_prod1_gateway_integration import (
    EXEC_BOUND,
    EXEC_CAP,
    EXEC_EVIDENCE,
    SYNTHETIC_EXEC_REVIEW,
    _no_network,  # noqa: F401 - autouse: any non-loopback connection fails the test
    adapter,
    exec_harness,
)
from tests.test_prod1_s5a_authority_resume import TERMINAL, Flow, Scripted, TwoWaves

ROOT = Path(__file__).resolve().parents[1]
ENVELOPE = ProviderEnvelope(members=(EXEC_BOUND,))


def capability(**changes: Any) -> Any:
    values: dict[str, Any] = {
        "bound": EXEC_BOUND,
        "evidence_version": SYNTHETIC_EXEC_REVIEW,
        "provider_deadline_enforced": True,
        "termination_confirmed": True,
        "usage_reported": True,
    }
    values.update(changes)
    return ReviewedExecutionCapability(**values)


def evidence(*items: Any, version: str = SYNTHETIC_EXEC_REVIEW) -> ReviewedExecutionEvidence:
    return ReviewedExecutionEvidence(
        evidence_version=version, capabilities=items or (capability(),)
    )


def real_adapter(cap: int = EXEC_CAP) -> Any:
    return adapter(Provider(Clock()), cap=cap)


# --- 1-12: the reviewed execution-capability contract and strict admission ---------------------


def test_a_reviewed_deadline_adapter_is_admitted_on_the_strict_path() -> None:
    model = real_adapter()
    assert admitted_model_adapter(model, ENVELOPE, evidence=EXEC_EVIDENCE) is model
    assert admit_execution_adapter(model, envelope=ENVELOPE, capability=capability()) is model


def test_a_plain_bounded_adapter_is_refused_even_with_evidence_for_its_bound() -> None:
    plain = ExactAdapter()  # no deadline method: Amendment 11 item 3
    exact = evidence(capability(bound=EXACT_BOUND))
    assert admitted_model_adapter(plain, EXACT_ENVELOPE, evidence=exact) is None
    with pytest.raises(MeteringUnbounded):
        admit_execution_adapter(plain, envelope=EXACT_ENVELOPE, capability=exact.capabilities[0])


@pytest.mark.parametrize(
    "reviewed",
    [
        None,  # no evidence, no opt-in
        evidence(capability(bound=EXEC_BOUND.model_copy(update={"provider": "azure"}))),
        evidence(
            capability(bound=EXEC_BOUND.model_copy(update={"model": "test-model-2099-02-02"}))
        ),
        evidence(capability(bound=EXEC_BOUND.model_copy(update={"max_output_tokens": 2_000}))),
        evidence(capability(bound=EXEC_BOUND.model_copy(update={"data_recipient": "Other"}))),
    ],
    ids=["missing", "wrong-provider", "wrong-snapshot", "wrong-cap", "wrong-recipient"],
)
def test_evidence_for_any_other_member_never_applies(reviewed: Any) -> None:
    assert admitted_model_adapter(real_adapter(), ENVELOPE, evidence=reviewed) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_deadline_enforced": False},
        {"termination_confirmed": False},
        {"usage_reported": False},
        {"provider_deadline_enforced": 1},  # truthy is not reviewed
        {"termination_confirmed": None},
        {"bound": EXEC_BOUND.model_copy(update={"max_output_tokens": None})},
        {"bound": EXEC_BOUND.model_copy(update={"max_attempts": 2})},
        {"bound": EXEC_BOUND.model_copy(update={"input_bounded": False})},
        {"evidence_version": ""},
    ],
)
def test_incomplete_or_unreviewed_capability_evidence_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        capability(**changes)


def test_stale_or_inconsistent_evidence_sets_are_rejected() -> None:
    with pytest.raises(ValidationError):  # a stale member under a newer set version
        evidence(capability(), version="synthetic-exec-review-2")
    with pytest.raises(ValidationError):  # the same provider/model reviewed twice
        evidence(
            capability(), capability(bound=EXEC_BOUND.model_copy(update={"max_output_tokens": 9}))
        )
    with pytest.raises(ValidationError):
        ReviewedExecutionEvidence(evidence_version=SYNTHETIC_EXEC_REVIEW, capabilities=())


def test_a_substituted_capability_or_evidence_object_fails_closed() -> None:
    model = real_adapter()
    forged = ReviewedExecutionCapability.model_construct(
        bound=EXEC_BOUND,
        evidence_version=SYNTHETIC_EXEC_REVIEW,
        provider_deadline_enforced=True,
        termination_confirmed=False,  # skipped validation
        usage_reported=True,
    )
    with pytest.raises(MeteringUnbounded):
        admit_execution_adapter(model, envelope=ENVELOPE, capability=forged)

    class Lenient(ReviewedExecutionEvidence):
        pass

    lenient = Lenient(evidence_version=SYNTHETIC_EXEC_REVIEW, capabilities=(capability(),))
    assert admitted_model_adapter(model, ENVELOPE, evidence=lenient) is None
    with pytest.raises(TypeError):
        InvestigationToolGateway(**_gateway_kwargs(execution_evidence=lenient))


def test_an_adapter_whose_identity_differs_from_its_bound_is_refused() -> None:
    model = real_adapter()

    class Liar(type(model)):  # type: ignore[misc]
        @property
        def model(self) -> str:
            return "test-model-2099-02-02"

    liar = Liar.__new__(Liar)
    liar.__dict__.update(model.__dict__)
    with pytest.raises(MeteringUnbounded):
        admit_execution_adapter(liar, envelope=ENVELOPE, capability=capability())


# --- 13-22, 25: the deadline and cancellation guarantees run on the strict path ----------------


def test_the_s3_execution_suite_runs_through_strict_reviewed_admission() -> None:
    h, _, model = exec_harness(counted(400))
    gateway = h.gateway
    assert gateway._scripted_admission is None
    assert gateway._execution_evidence is EXEC_EVIDENCE
    assert gateway._model_admission()[0] is model


def test_an_expired_ledger_sends_nothing_even_when_admitted() -> None:
    h, provider, _ = exec_harness(counted(400))
    h.ledger._clock = lambda: 10_000_000.0  # far past the 240-second execution ledger
    started = h.begin()
    if started.attempt is not None:
        h.research(started.attempt)
    assert provider.calls == []


# --- 23-24: no production fallback; the scripted path is explicit and test-only ---------------


def _gateway_kwargs(h: Any = None, **overrides: Any) -> dict[str, Any]:
    h = h or tool_harness()
    values: dict[str, Any] = {
        "authority": h.gateway._authority,
        "live": h.live,
        "clock": h.clock,
        "cancellation": h.cancellation,
        "ledger": h.ledger,
        "resolver": h.resolver,
        "llm_client": ExactAdapter(),
        "max_input_tokens_per_call": 4_000,
    }
    values.update(overrides)
    return values


def test_a_strict_gateway_refuses_a_scripted_adapter_without_the_opt_in() -> None:
    h = tool_harness()
    h.gateway = InvestigationToolGateway(**_gateway_kwargs(h, llm_client=h.llm))
    started = h.begin()
    result = h.research(started.attempt)
    assert result.refusal is RefusalCode.METERING_UNBOUNDED
    assert h.llm.prompts == []


def test_the_opt_in_cannot_be_forged_or_substituted() -> None:
    with pytest.raises(TypeError):
        ScriptedExecutionAdmission()  # only the one module-level instance exists
    with pytest.raises(TypeError):
        InvestigationToolGateway(**_gateway_kwargs(scripted_admission=object()))
    assert admitted_model_adapter(ExactAdapter(), EXACT_ENVELOPE, scripted=object()) is None  # type: ignore[arg-type]


def test_no_production_module_references_the_scripted_opt_in() -> None:
    offenders = []
    for folder in ("apps", "src", "packages", "config", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"\bSCRIPTED_EXECUTION_ADMISSION\b", text):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == ["src/orchestration/tool_gateway.py"]  # the definition alone


def test_configured_production_execution_never_uses_the_scripted_path() -> None:
    from tests.test_prod1_configured_adapters import config

    built = providers.ConfiguredInvestigationAdapters(
        config(), transport_factory=lambda: None
    ).execution()
    assert built.scripted_admission is None
    assert type(built.evidence) is ReviewedExecutionEvidence
    assert providers.REVIEWED_EXECUTION_EVIDENCE is None  # production holds no review


def test_production_defaults_admit_nothing() -> None:
    from apps.api.investigations import FailClosedAdapters

    fail_closed = FailClosedAdapters().execution()
    assert fail_closed.llm_client is None and fail_closed.evidence is None
    assert fail_closed.scripted_admission is None
    assert tool_gateway.SCRIPTED_EXECUTION_ADMISSION is SCRIPTED_EXECUTION_ADMISSION


# --- 26-27: consent at plan approval, and one execution provider per investigation -------------


class Counting(TwoWaves):
    """Records every ``execution()`` call and the provider it returned."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.execution_calls: list[Any] = []

    def execution(self) -> Any:
        if self.fail:
            self.execution_calls.append(None)
            raise RuntimeError("configuration unavailable")
        provider = super().execution()
        self.execution_calls.append(provider)
        return provider


def test_consent_withdrawn_before_approval_refuses_it_and_ends_without_execution() -> None:
    """Before S5b the approval succeeded (200) and execution then failed generically."""
    adapters = Counting()
    d = Deployment("s5b_plan_consent", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        body = flow.to_plan_approval()
        flow.withdraw()
        refused = _approve(client, body)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "authority_revoked"
        ended = flow.until(*TERMINAL)
        assert ended["status"] == "failed" and ended["explanation_code"] == "authority_revoked"
        assert adapters.llm.counts == 0 and adapters.llm.sql_prompts == []
        assert len(adapters.execution_calls) == 1  # resolved once, to learn its recipients
        assert "agent.investigation_approved" not in [e.name for e in d.audit.events]
        assert flow.live().grant is None and flow.live().run is None


def test_an_investigation_without_hosted_recipients_needs_no_consent_to_be_approved() -> None:
    class Local(Scripted):
        @property
        def bound(self) -> Any:
            return EXACT_BOUND.model_copy(update={"data_recipient": None})

    class LocalAdapters(Counting):
        def __init__(self) -> None:
            super().__init__()
            self.llm = Local(script=self.llm.script)

        def execution(self) -> Any:
            provider = super().execution()
            local = ProviderEnvelope(members=(self.llm.bound,))
            return type(provider)(
                llm_client=self.llm,
                envelope=local,
                max_input_tokens_per_call=provider.max_input_tokens_per_call,
                scripted_admission=SCRIPTED_EXECUTION_ADMISSION,
            )

    adapters = LocalAdapters()
    d = Deployment("s5b_local", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        body = flow.to_plan_approval()
        flow.withdraw()  # withdraws only the hosted recipient this run never uses
        assert _approve(client, body).status_code == 200
        paused = flow.until("awaiting_sql_approval", *TERMINAL)
        assert paused["status"] == "awaiting_sql_approval", paused
        assert len(adapters.execution_calls) == 1


def test_one_execution_provider_serves_approval_execution_and_resume() -> None:
    adapters = Counting()
    d = Deployment("s5b_once", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        body = flow.to_plan_approval()
        assert _approve(client, body, plan_digest="0" * 64).status_code == 409  # refused attempt
        assert len(adapters.execution_calls) == 1
        assert _approve(client, body).status_code == 200
        flow.until("awaiting_sql_approval", *TERMINAL)
        first = flow.wave_id()
        assert _approve_wave(client, flow.id).status_code == 202
        flow.after_resume(first)
        assert len(adapters.execution_calls) == 1  # never rebuilt for execute or resume
        assert flow.live().execution is adapters.execution_calls[0]
        assert flow.live().run._gateway._llm_client is adapters.execution_calls[0].llm_client


def test_a_failed_provider_resolution_is_never_retried_and_fails_closed() -> None:
    adapters = Counting(fail=True)
    d = Deployment("s5b_unavailable", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = Flow(client, d)
        body = flow.to_plan_approval()
        assert _approve(client, body).status_code == 200
        ended = flow.until(*TERMINAL)
        assert ended["status"] == "failed"
        assert ended["explanation_code"] == "execution_provider_unavailable"
        assert adapters.execution_calls == [None]  # exactly one attempt, no replacement


class KeyedFlow(Flow):
    """A second investigation in the same deployment needs its own idempotency key."""

    def __init__(self, client: TestClient, deployment: Deployment, *, key: str) -> None:
        self.client = client
        self.deployment = deployment
        self.workspace_id, conversation_id, message_id = _proposal(client)
        self.id = _create(client, conversation_id, message_id, key=key).json()["id"]


def test_two_investigations_never_share_an_execution_provider() -> None:
    adapters = Counting()
    d = Deployment("s5b_two", adapters=adapters)
    with TestClient(d.app()) as client:
        flows = [Flow(client, d), KeyedFlow(client, d, key="k2")]
        for flow in flows:
            assert _approve(client, flow.to_plan_approval()).status_code == 200
            flow.until("awaiting_sql_approval", *TERMINAL)
        first, second = (flow.live().execution for flow in flows)
        assert first is not None and second is not None and first is not second
        assert len(adapters.execution_calls) == 2


# --- 28-30: no leakage, no network, routing off --------------------------------------------


def test_admission_refusals_log_no_key_prompt_or_evidence(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    h, _, model = exec_harness(counted(400))
    admitted_model_adapter(model, ENVELOPE, evidence=evidence(capability(bound=EXACT_BOUND)))
    rendered = caplog.text + repr(model) + repr(EXEC_EVIDENCE.model_dump())
    assert FAKE_KEY not in rendered


def test_investigation_routing_stays_off_by_default() -> None:
    from tests.test_api_features import _settings

    settings = _settings(ROOT / "unused")
    assert settings.investigation_routing_enabled is False
    assert settings.investigation_model_activation is False
    assert providers.LIVE_ADMISSION_RELEASED is False
