"""SEC-1 (#157): every investigation route, enumerated from the app so a new one cannot be skipped.

Tenant isolation, capability, consent, and rate limits run through the real API, runtime, approval
service, gateways, and job executor with scripted bounded adapters. No network, key, or provider
account is used. These tests show the server's controls, not how any real model behaves.

Adding an investigation route fails ``test_every_route_is_classified`` until it has a row in
``CAPABILITY``, ``RATE_LIMIT``, and ``CONSENT``. A body-less route gets the tenant test
automatically.

The last section characterises two open PROD-1 review findings: it pins today's behaviour, including
the part that is wrong, so a fix must change these tests on purpose.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.rate_limit import InMemoryRateLimiter, RateLimitDecision
from packages.governance import InMemoryAuditSink
from packages.observability import InMemoryTelemetrySink
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_api_investigations import (
    OTHER_TENANT,
    QUESTION,
    Deployment,
    ScriptedAdapters,
    _as,
    _create,
    _proposal,
    _status,
    _until,
)
from tests.test_prod1_gateway_integration import (
    _no_network,  # noqa: F401 - autouse: any non-loopback connection fails the test
)
from tests.test_prod1_s5a_authority_resume import TERMINAL, Flow, RecipientPlanning, TwoWaves
from tests.test_utils import isolated_directory_path

CREATE = ("POST", "/api/v1/conversations/{conversation_id}/investigations")
BY_MESSAGE = (
    "GET",
    "/api/v1/conversations/{conversation_id}/investigations/by-message/{message_id}",
)
READ = ("GET", "/api/v1/investigations/{investigation_id}")
PLAN_DECISION = ("POST", "/api/v1/investigations/{investigation_id}/plan-decision")
CANCEL = ("POST", "/api/v1/investigations/{investigation_id}/cancel")
WAVE_READ = ("GET", "/api/v1/investigations/{investigation_id}/sql-wave")
WAVE_DECISION = ("POST", "/api/v1/investigations/{investigation_id}/sql-wave")

# What each route requires of a verified identity.
CAPABILITY = {
    CREATE: "analysis:run",
    BY_MESSAGE: "workspace:read",
    READ: "workspace:read",
    PLAN_DECISION: "analysis:run",
    CANCEL: "analysis:run",
    WAVE_READ: "analysis:run",
    WAVE_DECISION: "analysis:run",
}
# Which limiter operation a route spends. ``None`` is a decision: see the rate-limit section.
RATE_LIMIT = {
    CREATE: "investigation",
    BY_MESSAGE: None,
    READ: None,
    PLAN_DECISION: "investigation_decision",
    CANCEL: None,
    WAVE_READ: None,
    WAVE_DECISION: "investigation_decision",
}
# What a route does once consent is withdrawn; each is proved below.
CONSENT = {
    CREATE: "test_withdrawn_consent_lets_create_record_but_planning_sends_nothing",
    BY_MESSAGE: "test_withdrawn_consent_leaves_reads_available_and_sends_nothing",
    READ: "test_withdrawn_consent_leaves_reads_available_and_sends_nothing",
    PLAN_DECISION: "test_withdrawn_consent_refuses_a_plan_approval_and_sends_nothing",
    CANCEL: "test_withdrawn_consent_never_blocks_cancel",
    WAVE_READ: "test_withdrawn_consent_leaves_reads_available_and_sends_nothing",
    WAVE_DECISION: "test_withdrawn_consent_refuses_a_wave_decision_and_runs_nothing",
}


def _routes(app: FastAPI) -> set[tuple[str, str]]:
    """From the published contract: ``app.routes`` hides routes inside included routers."""
    found = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "investigation" in path
        for method in operations
    }
    assert found  # an empty enumeration would make every loop below pass without testing anything
    return found


def test_every_route_is_classified() -> None:
    routes = _routes(Deployment("sec1_routes").app())
    for table in (CAPABILITY, RATE_LIMIT, CONSENT):
        assert routes == set(table), "classify the new investigation route in this module"
    for test_name in CONSENT.values():
        assert test_name in globals()


def _url(route: tuple[str, str], flow: Flow, conversation: str, message: str) -> str:
    path = route[1]
    for name, value in (
        ("{investigation_id}", flow.id),
        ("{conversation_id}", conversation),
        ("{message_id}", message),
    ):
        path = path.replace(name, value)
    assert "{" not in path or route not in CAPABILITY, path
    return path.replace("{", "").replace("}", "")  # a future parameter: any unknown value


def _valid_bodies(client: TestClient, flow: Flow, message: str) -> dict[tuple[str, str], Any]:
    """What the owner could really send, so a foreign caller holds every correct identifier."""
    wave = client.get(f"/api/v1/investigations/{flow.id}/sql-wave").json()
    body = flow.body()
    return {
        CREATE: {"message_id": message},
        PLAN_DECISION: {
            "decision": "approve",
            "plan_digest": body["plan_digest"],
            "authority_epoch": body["authority_epoch"],
        },
        WAVE_DECISION: {
            "wave_id": wave["wave_id"],
            "wave_digest": wave["wave_digest"],
            "items": [
                {
                    "task_id": item["task_id"],
                    "sql_fingerprint": item["sql_fingerprint"],
                    "decision": "approve",
                }
                for item in wave["items"]
            ],
        },
    }


def _paused(name: str, *, adapters: Any = None) -> tuple[Deployment, FastAPI]:
    deployment = Deployment(name, adapters=adapters or TwoWaves())
    return deployment, deployment.app()


def _send(
    client: TestClient, route: tuple[str, str], url: str, body: Any, key: str = "sec1"
) -> Any:
    return client.request(route[0], url, json=body, headers={"Idempotency-Key": key})


# --- tenant isolation -----------------------------------------------------------------------------


def test_a_foreign_tenant_gets_the_same_404_on_every_route_and_changes_nothing() -> None:
    deployment, app = _paused("sec1_tenant")
    with TestClient(app) as client:
        flow = Flow(client, deployment)
        flow.to_first_wave()
        conversation = flow.body()["conversation_id"]
        message = flow.body()["message_id"]
        bodies = _valid_bodies(client, flow, message)
        before = flow.body()
        calls = len(deployment.adapters.llm.sql_prompts)
        statement = client.get(f"/api/v1/investigations/{flow.id}/sql-wave").json()
        secret = statement["items"][0]["sql"]

        _as(app, OTHER_TENANT)
        for route in sorted(_routes(app)):
            url = _url(route, flow, conversation, message)
            answer = _send(client, route, url, bodies.get(route))
            assert answer.status_code == 404, (route, answer.text)
            for private in (secret, flow.id, statement["wave_digest"], QUESTION):
                assert private not in answer.text, route
        unknown = client.get("/api/v1/investigations/inv_unknown")
        foreign = client.get(f"/api/v1/investigations/{flow.id}")
        assert unknown.status_code == foreign.status_code == 404
        strip = ("request_id",)
        assert _error(unknown, strip) == _error(foreign, strip)  # a foreign ID reads as unknown
        job = client.get(f"/api/v1/jobs/{before['job_id']}")
        assert job.status_code == 404
        app.dependency_overrides.clear()

        after = flow.body()
        assert after["status"] == before["status"] == "awaiting_sql_approval"
        assert after["sql_wave"] == before["sql_wave"]
        assert len(deployment.adapters.llm.sql_prompts) == calls
        assert "agent.sql_approved" not in [event.name for event in deployment.audit.events]


def _error(answer: Any, strip: tuple[str, ...]) -> dict[str, Any]:
    return {key: value for key, value in answer.json()["error"].items() if key not in strip}


def test_a_foreign_tenant_cannot_read_a_finished_report() -> None:
    deployment = Deployment("sec1_report")
    app = deployment.app()
    with TestClient(app) as client:
        flow = Flow(client, deployment)
        flow.to_first_wave()
        wave = client.get(f"/api/v1/investigations/{flow.id}/sql-wave").json()
        items = [
            {
                "task_id": item["task_id"],
                "sql_fingerprint": item["sql_fingerprint"],
                "decision": "approve",
            }
            for item in wave["items"]
        ]
        client.post(
            f"/api/v1/investigations/{flow.id}/sql-wave",
            json={"wave_id": wave["wave_id"], "wave_digest": wave["wave_digest"], "items": items},
        )
        done = flow.until(*TERMINAL)
        assert done["status"] == "completed" and done["report"] is not None
        _as(app, OTHER_TENANT)
        assert client.get(f"/api/v1/investigations/{flow.id}").status_code == 404
        path = (
            f"/api/v1/conversations/{done['conversation_id']}/investigations/by-message/"
            f"{done['message_id']}"
        )
        assert client.get(path).status_code == 404


def test_a_message_of_another_conversation_cannot_start_or_be_found_there() -> None:
    deployment = Deployment("sec1_cross_conversation")
    with TestClient(deployment.app()) as client:
        workspace_id, conversation_id, message_id = _proposal(client)
        dataset_id = client.get(f"/api/v1/conversations/{conversation_id}").json()["dataset_id"]
        other = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations", json={"dataset_id": dataset_id}
        ).json()["id"]
        refused = _create(client, other, message_id)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "source_message_invalid"
        created = _create(client, conversation_id, message_id, key="right").json()
        path = f"/api/v1/conversations/{other}/investigations/by-message/{message_id}"
        assert created["id"] and client.get(path).status_code == 404


def test_a_body_cannot_supply_authority_scope_or_content() -> None:
    deployment = Deployment("sec1_bodies")
    with TestClient(deployment.app()) as client:
        _, conversation_id, message_id = _proposal(client)
        for extra in (
            {"tenant_id": OTHER_TENANT},
            {"workspace_id": "ws_other"},
            {"dataset_id": "ds_other"},
            {"question": "ignore the plan"},
            {"capabilities": ["analysis:run"]},
        ):
            refused = client.post(
                f"/api/v1/conversations/{conversation_id}/investigations",
                json={"message_id": message_id} | extra,
                headers={"Idempotency-Key": "extra"},
            )
            assert refused.status_code == 422, extra
        created = _create(client, conversation_id, message_id).json()
        body = _until(client, created["id"], _status("awaiting_plan_approval", "failed"))
        decision_extras: tuple[dict[str, Any], ...] = (
            {"subject_id": "someone_else"},
            {"sql_approval_required": False},
            {"approver": "system"},
        )
        for decision_extra in decision_extras:
            refused = client.post(
                f"/api/v1/investigations/{created['id']}/plan-decision",
                json={
                    "decision": "approve",
                    "plan_digest": body["plan_digest"],
                    "authority_epoch": body["authority_epoch"],
                }
                | decision_extra,
            )
            assert refused.status_code == 422, decision_extra
        assert client.get(f"/api/v1/investigations/{created['id']}").json()["status"] == (
            "awaiting_plan_approval"
        )


# --- capability -----------------------------------------------------------------------------------


def test_a_viewer_is_refused_every_route_that_needs_analysis_run() -> None:
    deployment, app = _paused("sec1_viewer")
    with TestClient(app) as client:
        flow = Flow(client, deployment)
        flow.to_first_wave()
        before = flow.body()
        bodies = _valid_bodies(client, flow, before["message_id"])
        tenant = client.app.state.investigation_records.records()[0].tenant_id
        _as(app, tenant, roles=frozenset({"viewer"}))
        for route, needed in CAPABILITY.items():
            url = _url(route, flow, before["conversation_id"], before["message_id"])
            answer = _send(client, route, url, bodies.get(route))
            assert answer.status_code == (403 if needed == "analysis:run" else 200), route
        app.dependency_overrides.clear()
        after = flow.body()
        assert after["status"] == before["status"] and after["sql_wave"] == before["sql_wave"]


# --- consent --------------------------------------------------------------------------------------


def _withdraw(client: TestClient, flow: Flow) -> None:
    answer = client.delete(f"/api/v1/workspaces/{flow.workspace_id}/consent")
    assert answer.status_code == 200 and answer.json()["data_recipients"] == []


def test_withdrawn_consent_lets_create_record_but_planning_sends_nothing() -> None:
    adapters = RecipientPlanning()
    deployment, app = _paused("sec1_consent_create", adapters=adapters)
    with TestClient(app) as client:
        workspace_id, conversation_id, message_id = _proposal(client)
        assert client.delete(f"/api/v1/workspaces/{workspace_id}/consent").status_code == 200
        created = _create(client, conversation_id, message_id)
        assert created.status_code == 202
        body = _until(client, created.json()["id"], lambda item: item["status"] != "planning")
        assert body["status"] == "failed", body
        assert body["explanation_code"] in {"consent_missing", "authority_revoked"}, body
        assert body["plan"] is None and body["report"] is None
        assert adapters.planner is not None and adapters.planner.calls == []


def test_withdrawn_consent_refuses_a_plan_approval_and_sends_nothing() -> None:
    deployment, app = _paused("sec1_consent_plan")
    with TestClient(app) as client:
        flow = Flow(client, deployment)
        body = flow.to_plan_approval()
        _withdraw(client, flow)
        refused = client.post(
            f"/api/v1/investigations/{flow.id}/plan-decision",
            json={
                "decision": "approve",
                "plan_digest": body["plan_digest"],
                "authority_epoch": body["authority_epoch"],
            },
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "authority_revoked"
        ended = flow.until(*TERMINAL)
        assert (ended["status"], ended["explanation_code"]) == ("failed", "authority_revoked")
        assert deployment.adapters.llm.sql_prompts == []


def test_withdrawn_consent_refuses_a_wave_decision_and_runs_nothing() -> None:
    deployment, app = _paused("sec1_consent_wave")
    with TestClient(app) as client:
        flow = Flow(client, deployment)
        flow.to_first_wave()
        body = _valid_bodies(client, flow, flow.body()["message_id"])[WAVE_DECISION]
        prompts = len(deployment.adapters.llm.sql_prompts)
        _withdraw(client, flow)
        refused = client.post(f"/api/v1/investigations/{flow.id}/sql-wave", json=body)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "authority_revoked"
        ended = flow.until(*TERMINAL)
        assert ended["status"] == "cancelled" and ended["explanation_code"] == "authority_revoked"
        assert len(deployment.adapters.llm.sql_prompts) == prompts
        assert "agent.sql_approved" not in [event.name for event in deployment.audit.events]


def test_withdrawn_consent_leaves_reads_available_and_sends_nothing() -> None:
    """A read shows what is already held here. It starts no work and sends nothing anywhere."""
    deployment, app = _paused("sec1_consent_reads")
    with TestClient(app) as client:
        flow = Flow(client, deployment)
        flow.to_first_wave()
        body = flow.body()
        prompts = len(deployment.adapters.llm.sql_prompts)
        _withdraw(client, flow)
        for route, _ in ((r, n) for r, n in RATE_LIMIT.items() if r[0] == "GET"):
            url = _url(route, flow, body["conversation_id"], body["message_id"])
            assert client.get(url).status_code == 200, route
        assert len(deployment.adapters.llm.sql_prompts) == prompts


def test_withdrawn_consent_never_blocks_cancel() -> None:
    deployment, app = _paused("sec1_consent_cancel")
    with TestClient(app) as client:
        flow = Flow(client, deployment)
        flow.to_first_wave()
        prompts = len(deployment.adapters.llm.sql_prompts)
        _withdraw(client, flow)
        assert flow.cancel().status_code == 202
        assert flow.until(*TERMINAL)["status"] == "cancelled"
        assert len(deployment.adapters.llm.sql_prompts) == prompts


# --- rate limits ----------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class SpyLimiter(InMemoryRateLimiter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.operations: list[str] = []

    def consume(self, *, operation: str, **kwargs: Any) -> RateLimitDecision:
        self.operations.append(operation)
        return super().consume(operation=operation, **kwargs)


class Limited:
    """A deployment with exact limits; ``telemetry`` is the real sink, ``limiter`` optional."""

    def __init__(self, name: str, *, limiter: InMemoryRateLimiter | None = None, **limits: Any):
        self.adapters = ScriptedAdapters()
        self.audit = InMemoryAuditSink()
        self.telemetry = InMemoryTelemetrySink()
        settings = replace(
            _settings(isolated_directory_path(name)),
            durable_metadata=True,
            investigation_routing_enabled=True,
            rate_limit_window_seconds=30,
            **limits,
        )
        self.application = create_app(
            settings=settings,
            embedder_factory=lambda _model: FakeEmbedder(),
            llm_client_factory=lambda _settings: FakeLLM(),
            telemetry_sink=self.telemetry,
            audit_sink=self.audit,
            investigation_adapters=self.adapters,
            rate_limiter=limiter,
        )


def _assert_429(answer: Any) -> None:
    assert answer.status_code == 429, answer.text
    assert 1 <= int(answer.headers["Retry-After"]) <= 30  # bounded by the window
    assert answer.headers["X-Request-ID"] == answer.json()["error"]["request_id"]
    assert answer.json()["error"]["code"] == "rate_limit_exceeded"
    assert answer.json()["error"]["message"] == (
        "Too many requests for this operation. Try again later."
    )


def _second_proposal(client: TestClient, conversation_id: str) -> str:
    return str(
        client.post(
            f"/api/v1/conversations/{conversation_id}/messages", json={"question": QUESTION}
        ).json()["id"]
    )


def test_each_route_spends_exactly_its_own_limit() -> None:
    limiter = SpyLimiter()
    deployment = Limited("sec1_rate_policy", limiter=limiter)
    with TestClient(deployment.application) as client:
        _, conversation_id, message_id = _proposal(client)
        created = _create(client, conversation_id, message_id).json()
        _until(client, created["id"], _status("awaiting_plan_approval", "failed"))
        digest = {"decision": "approve", "plan_digest": "0" * 64, "authority_epoch": 1}
        wave = {
            "wave_id": "0" * 32,
            "wave_digest": "0" * 64,
            "items": [
                {"task_id": "trend", "sql_fingerprint": "0" * 64, "decision": "approve"},
            ],
        }
        bodies = {CREATE: {"message_id": message_id}, PLAN_DECISION: digest, WAVE_DECISION: wave}
        investigation = created["id"]
        for route, operation in RATE_LIMIT.items():
            url = (
                route[1]
                .replace("{investigation_id}", investigation)
                .replace("{conversation_id}", conversation_id)
                .replace("{message_id}", message_id)
            )
            limiter.operations.clear()
            _send(client, route, url, bodies.get(route), key="spend")
            assert limiter.operations == ([] if operation is None else [operation]), route


def test_a_429_on_create_is_bounded_and_starts_nothing() -> None:
    deployment = Limited("sec1_rate_create", rate_limit_investigations=1)
    with TestClient(deployment.application) as client:
        _, conversation_id, message_id = _proposal(client)
        assert _create(client, conversation_id, message_id).status_code == 202
        records = client.app.state.investigation_records.records()
        jobs, _ = client.app.state.job_executor.list_page(records[0].tenant_id, limit=100)
        other = _second_proposal(client, conversation_id)
        limited = _create(client, conversation_id, other, key="second")
        _assert_429(limited)
        ids = [record.id for record in client.app.state.investigation_records.records()]
        assert ids == [record.id for record in records]
        after, _ = client.app.state.job_executor.list_page(records[0].tenant_id, limit=100)
        assert [job.id for job in after] == [job.id for job in jobs]
    decisions = deployment.telemetry.named("rate_limit.decision")
    mine = [e for e in decisions if e.attributes["rate_limit_operation"] == "investigation"]
    assert [event.attributes["rate_limited"] for event in mine] == [False, True]
    assert all(event.dropped_attributes == 0 for event in mine)
    text = json.dumps([event.attributes for event in decisions])
    assert QUESTION not in text and conversation_id not in text


def test_a_decision_limit_refuses_without_changing_the_investigation_then_resets() -> None:
    clock = Clock()
    deployment = Limited(
        "sec1_rate_decision",
        limiter=InMemoryRateLimiter(clock=clock),
        rate_limit_investigation_decisions=1,
    )
    with TestClient(deployment.application) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation, _status("awaiting_plan_approval", "failed"))
        url = f"/api/v1/investigations/{investigation}/plan-decision"
        good = {
            "decision": "approve",
            "plan_digest": body["plan_digest"],
            "authority_epoch": body["authority_epoch"],
        }
        assert client.post(url, json=good | {"plan_digest": "0" * 64}).status_code == 409
        _assert_429(client.post(url, json=good))  # a wrong guess still spends the decision limit
        unchanged = client.get(f"/api/v1/investigations/{investigation}").json()
        assert unchanged["status"] == "awaiting_plan_approval"
        assert deployment.adapters.llm.sql_prompts == []
        clock.now += 30
        assert client.post(url, json=good).status_code == 200  # the same approval still works


def test_a_limit_never_blocks_cancel_or_reads() -> None:
    deployment = Limited(
        "sec1_rate_exempt", rate_limit_investigations=1, rate_limit_investigation_decisions=1
    )
    with TestClient(deployment.application) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation = _create(client, conversation_id, message_id).json()["id"]
        _until(client, investigation, _status("awaiting_plan_approval", "failed"))
        url = f"/api/v1/investigations/{investigation}"
        _assert_429(_create(client, conversation_id, _second_proposal(client, conversation_id)))
        decision = {"decision": "reject", "plan_digest": "0" * 64, "authority_epoch": 1}
        client.post(f"{url}/plan-decision", json=decision)
        _assert_429(client.post(f"{url}/plan-decision", json=decision))
        for _ in range(5):
            assert client.get(url).status_code == 200
        assert client.post(f"{url}/cancel").status_code == 202
        assert client.get(url).json()["status"] == "cancelled"


def test_a_foreign_tenant_gets_404_and_spends_nothing_even_at_the_limit() -> None:
    limiter = SpyLimiter()
    deployment = Limited(
        "sec1_rate_tenant",
        limiter=limiter,
        rate_limit_investigations=1,
        rate_limit_investigation_decisions=1,
    )
    app = deployment.application
    with TestClient(app) as client:
        _, conversation_id, message_id = _proposal(client)
        investigation = _create(client, conversation_id, message_id).json()["id"]
        body = _until(client, investigation, _status("awaiting_plan_approval", "failed"))
        url = f"/api/v1/investigations/{investigation}/plan-decision"
        good = {
            "decision": "approve",
            "plan_digest": body["plan_digest"],
            "authority_epoch": body["authority_epoch"],
        }
        limiter.operations.clear()
        _as(app, OTHER_TENANT)
        for _ in range(3):  # at the owner's limit too: a 429 here would reveal the ID exists
            assert client.post(url, json=good).status_code == 404
            assert _create(client, conversation_id, message_id, key="x").status_code == 404
        assert limiter.operations == []  # an attacker cannot exhaust the owner's counters
        app.dependency_overrides.clear()
        assert client.post(url, json=good).status_code == 200


def test_limits_are_isolated_per_workspace_and_per_operation() -> None:
    deployment = Limited(
        "sec1_rate_scope", rate_limit_investigations=1, rate_limit_investigation_decisions=1
    )
    with TestClient(deployment.application) as client:
        _, first_conversation, first_message = _proposal(client)
        first = _create(client, first_conversation, first_message)
        assert first.status_code == 202
        _, second_conversation, second_message = _proposal(client)
        assert _create(client, second_conversation, second_message, key="k2").status_code == 202
        # The create limit is spent in both workspaces; each one's decision limit is untouched.
        body = _until(client, first.json()["id"], _status("awaiting_plan_approval", "failed"))
        answer = client.post(
            f"/api/v1/investigations/{first.json()['id']}/plan-decision",
            json={
                "decision": "reject",
                "plan_digest": body["plan_digest"],
                "authority_epoch": body["authority_epoch"],
            },
        )
        assert answer.status_code == 200


def test_a_disabled_feature_answers_as_disabled_and_spends_nothing() -> None:
    limiter = SpyLimiter()
    settings = replace(
        _settings(isolated_directory_path("sec1_rate_off")),
        durable_metadata=True,
        investigation_routing_enabled=False,
    )
    app = create_app(
        settings=settings,
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=InMemoryTelemetrySink(),
        audit_sink=InMemoryAuditSink(),
        rate_limiter=limiter,
    )
    with TestClient(app) as client:
        refused = _create(client, "conv_any", "msg_any")
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "investigations_disabled"
    assert limiter.operations == []


@pytest.mark.parametrize(
    ("variable", "attribute"),
    [
        ("RATE_LIMIT_INVESTIGATIONS", "rate_limit_investigations"),
        ("RATE_LIMIT_INVESTIGATION_DECISIONS", "rate_limit_investigation_decisions"),
    ],
)
def test_the_limits_are_configured_from_the_environment_and_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, variable: str, attribute: str
) -> None:
    from config.settings import get_settings

    monkeypatch.setenv(variable, "7")
    assert getattr(get_settings(), attribute) == 7
    monkeypatch.setenv(variable, "0")
    with pytest.raises(ValueError):
        get_settings()


# --- characterisation of open PROD-1 findings: not endorsed behaviour -----------------------------


def test_finding_1_digest_free_legacy_acceptance_records_recipients_never_shown() -> None:
    """OPEN (Medium). ``apps/api/feature_routes.py``: a digest-free ``2026-09`` acceptance skips the
    notice comparison, then records every non-OpenAI recipient configured now.

    A client that read the notice when only Gemini was configured therefore authorises Anthropic by
    re-sending that acceptance after the operator adds it. The re-acceptance rule was agreed, so
    this pins it until a decision changes it. OpenAI stays out, and a digest closes the gap.
    """
    settings = replace(
        _settings(isolated_directory_path("sec1_finding_1")),
        durable_metadata=True,
        llm_providers=("gemini", "anthropic"),
        anthropic_api_key="offline-test-placeholder-not-a-credential",
        investigation_model_provider="openai_responses",
        investigation_model_activation=True,
        openai_api_key="offline-test-placeholder-not-a-credential",
        investigation_model_snapshot="test-model-2099-01-01",
        investigation_model_review="review-placeholder-1",
    )
    app = create_app(
        settings=settings,
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=InMemoryTelemetrySink(),
        audit_sink=InMemoryAuditSink(),
    )
    with TestClient(app) as client:
        workspace_id = client.post("/api/v1/workspaces").json()["id"]
        url = f"/api/v1/workspaces/{workspace_id}/consent"
        digest_free = client.put(url, json={"accepted": True, "notice_version": "2026-09"})
        assert digest_free.status_code == 200
        assert digest_free.json()["data_recipients"] == ["Gemini", "Anthropic"]  # the finding
        assert "OpenAI" not in digest_free.json()["data_recipients"]  # the part that holds
        stale = client.put(
            url,
            json={"accepted": True, "notice_version": "2026-09", "notice_digest": "0" * 64},
        )
        assert stale.status_code == 409  # a client that sends a digest is protected
        assert stale.json()["error"]["code"] == "consent_notice_changed"


def test_finding_2_cancel_after_claim_release_leaves_a_stopped_pause_until_a_second_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPEN (Low; nothing is sent). ``apps/api/investigations.py``: ``_finish_run_job`` releases the
    claim, then ``_settle`` publishes the pause. A cancel between the two sees ``running`` and no
    claim, so nobody closes the run: the record shows a pause that can never proceed.
    """
    deployment, app = _paused("sec1_finding_2")
    with TestClient(app) as client:
        runtime = client.app.state.investigations
        settle = runtime._settle
        cancels: list[Any] = []

        def cancel_in_the_window(investigation_id: str, tenant: str, result: Any) -> None:
            if not cancels and result.status.value == "awaiting_sql_approval":
                assert not runtime._live[investigation_id].run_jobs  # the claim is already released
                cancels.append(runtime.cancel(tenant, investigation_id))
            settle(investigation_id, tenant, result)

        monkeypatch.setattr(runtime, "_settle", cancel_in_the_window)
        flow = Flow(client, deployment)
        body = flow.to_plan_approval()
        approved = client.post(
            f"/api/v1/investigations/{flow.id}/plan-decision",
            json={
                "decision": "approve",
                "plan_digest": body["plan_digest"],
                "authority_epoch": body["authority_epoch"],
            },
        )
        assert approved.status_code == 200
        stuck = flow.until("awaiting_sql_approval", *TERMINAL)
        assert cancels and cancels[0].status.value == "running"
        assert (
            stuck["status"] == "awaiting_sql_approval"
        )  # the finding: cancelled, yet shown paused

        prompts = len(deployment.adapters.llm.sql_prompts)
        # The part that holds: the stopped pause still shows its wave, but deciding it runs nothing.
        url = f"/api/v1/investigations/{flow.id}/sql-wave"
        wave = client.get(url).json()
        items = [
            {
                "task_id": item["task_id"],
                "sql_fingerprint": item["sql_fingerprint"],
                "decision": "approve",
            }
            for item in wave["items"]
        ]
        refused = client.post(
            url,
            json={"wave_id": wave["wave_id"], "wave_digest": wave["wave_digest"], "items": items},
        )
        assert refused.status_code == 409
        assert len(deployment.adapters.llm.sql_prompts) == prompts
        assert "agent.sql_approved" not in [event.name for event in deployment.audit.events]

        assert flow.cancel().status_code == 202  # the second cancel closes it
        assert flow.until(*TERMINAL)["status"] == "cancelled"
