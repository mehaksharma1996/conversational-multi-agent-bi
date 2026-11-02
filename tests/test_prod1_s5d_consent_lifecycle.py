"""PROD-1 S5d: the consent lifecycle: authoritative notice, version-safe acceptance, withdrawal.

Everything runs through the real API, repository, metadata store, approval service, and gateways
with scripted model clients. No network, key, or provider account is used, and accepting a notice
never admits a provider.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import investigation_providers as providers_module
from apps.api.consent_notices import (
    BASE_NOTICE,
    BASE_NOTICE_VERSION,
    NOTICES,
    OPENAI_NOTICE,
    OPENAI_NOTICE_VERSION,
    audit_recipients,
    consent_in_force,
    consent_status,
    effective_recipients,
    served_notice,
)
from apps.api.investigations import FailClosedAdapters, _consent
from apps.api.main import create_app
from apps.api.metadata_store import MIGRATIONS, MetadataStore
from packages.governance.audit import AUDIT_ACTIONS, AUDIT_ATTRIBUTES, InMemoryAuditSink
from packages.observability import InMemoryTelemetrySink
from src.llm.base import LLMResponse
from tests.test_api_features import FakeEmbedder, FakeLLM, _prepare_hybrid_context, _settings
from tests.test_api_investigations import Deployment, _approve, _approve_wave, _as
from tests.test_metadata_store import _workspace as _stored_workspace
from tests.test_openai_responses_bounded import FAKE_KEY, SNAPSHOT
from tests.test_prod1_gateway_integration import (
    _no_network,  # noqa: F401 - autouse: any non-loopback connection fails the test
)
from tests.test_prod1_s5a_authority_resume import RECIPIENT, TERMINAL, Flow, Gate, TwoWaves
from tests.test_utils import isolated_directory_path

REVIEW = "s5d-review-placeholder"
OTHER_TENANT = "b" * 32
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class CountingLLM(FakeLLM):
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> LLMResponse:
        self.calls += 1
        return super().generate(prompt)


class App:
    def __init__(self, name: str, *, openai: bool = False, **changes: Any) -> None:
        self.root = isolated_directory_path(name)
        self.audit = InMemoryAuditSink()
        self.llm = CountingLLM()
        values: dict[str, Any] = {"durable_metadata": True, **changes}
        if openai:
            values |= {
                "investigation_model_provider": "openai_responses",
                "investigation_model_activation": True,
                "openai_api_key": FAKE_KEY,
                "investigation_model_snapshot": SNAPSHOT,
                "investigation_model_review": REVIEW,
            }
        self.settings = replace(_settings(self.root), **values)

    def app(self) -> Any:
        return create_app(
            settings=self.settings,
            embedder_factory=lambda _model: FakeEmbedder(),
            llm_client_factory=lambda _settings: self.llm,
            telemetry_sink=InMemoryTelemetrySink(),
            audit_sink=self.audit,
        )

    def events(self, name: str) -> list[Any]:
        return [event for event in self.audit.events if event.name == name]


def _workspace(client: TestClient) -> str:
    return str(client.post("/api/v1/workspaces").json()["id"])


def _url(workspace_id: str) -> str:
    return f"/api/v1/workspaces/{workspace_id}/consent"


def _accept(client: TestClient, workspace_id: str, version: str, **extra: Any) -> Any:
    return client.put(
        _url(workspace_id), json={"accepted": True, "notice_version": version} | extra
    )


def _accept_current(client: TestClient, workspace_id: str) -> Any:
    notice = client.get(_url(workspace_id)).json()["notice"]
    return _accept(client, workspace_id, notice["version"], notice_digest=notice["digest"])


def _record(client: TestClient, workspace_id: str) -> Any:
    return client.app.state.repository._workspaces[workspace_id]


def _tamper(client: TestClient, workspace_id: str, **fields: Any) -> None:
    repository = client.app.state.repository
    repository._workspaces[workspace_id] = replace(repository._workspaces[workspace_id], **fields)


# --- the authoritative notice ----------------------------------------------------------------


def test_the_notice_is_served_with_status_recipients_and_no_store() -> None:
    with TestClient(App("s5d_notice").app()) as client:
        answer = client.get(_url(_workspace(client)))
    assert answer.status_code == 200
    assert answer.headers["Cache-Control"] == "no-store"
    body = answer.json()
    assert (body["status"], body["accepted"], body["required"]) == ("missing", False, True)
    assert body["questions_enabled"] is False and body["data_recipients"] == []
    notice = body["notice"]
    assert notice["version"] == BASE_NOTICE_VERSION and notice["recipients"] == ["Gemini"]
    assert notice["title"] == BASE_NOTICE.title
    assert [section["body"] for section in notice["sections"]] == [
        section.body for section in BASE_NOTICE.sections
    ]
    assert len(notice["digest"]) == 64


def test_the_openai_notice_discloses_processing_and_retention_without_implying_activation() -> None:
    with TestClient(App("s5d_openai_notice", openai=True).app()) as client:
        body = client.get(_url(_workspace(client))).json()
    notice = body["notice"]
    assert notice["version"] == OPENAI_NOTICE_VERSION
    assert notice["recipients"] == ["Gemini", "OpenAI"]
    text = " ".join(section["body"] for section in notice["sections"])
    for phrase in (
        "external processor",
        "query results",
        "uploaded documents",
        "background mode",
        "about ten minutes",
        "not a guaranteed deletion deadline",
        "does not turn on OpenAI processing",
        "does not recall a request already sent",
    ):
        assert phrase in text
    rendered = json.dumps(body)
    for private in (FAKE_KEY, SNAPSHOT, REVIEW):
        assert private not in rendered


def test_no_recipient_means_no_notice_and_nothing_to_accept() -> None:
    with TestClient(App("s5d_local", local_only_mode=True).app()) as client:
        body = client.get(_url(_workspace(client))).json()
    assert body["status"] == "not_required" and body["required"] is False
    assert body["notice"] is None and body["questions_enabled"] is True


def test_every_consent_route_is_tenant_scoped() -> None:
    deployment = App("s5d_tenant", openai=True)
    app = deployment.app()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        assert _accept_current(client, workspace_id).status_code == 200
        _as(app, OTHER_TENANT)
        assert client.get(_url(workspace_id)).status_code == 404
        assert client.delete(_url(workspace_id)).status_code == 404
        assert _accept(client, workspace_id, OPENAI_NOTICE_VERSION).status_code == 404
        assert _accept(client, workspace_id, "not-a-version").status_code == 404  # 404, not 409
        app.dependency_overrides.clear()
        assert _record(client, workspace_id).consent_accepted_at is not None
    assert len(deployment.events("consent.accepted")) == 1
    assert deployment.events("consent.withdrawn") == []


@pytest.mark.parametrize(
    ("roles", "read", "write"),
    [
        (frozenset({"viewer"}), 200, 403),
        (frozenset({"analyst"}), 200, 403),
        (frozenset({"workspace_admin"}), 200, 200),
    ],
)
def test_reading_needs_read_and_changing_needs_workspace_admin(
    roles: frozenset[str], read: int, write: int
) -> None:
    app = App(f"s5d_roles_{sorted(roles)[0]}").app()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        tenant = _record(client, workspace_id).tenant_id
        _as(app, tenant, roles=roles)
        assert client.get(_url(workspace_id)).status_code == read
        assert _accept(client, workspace_id, BASE_NOTICE_VERSION).status_code == write
        assert client.delete(_url(workspace_id)).status_code == write
        app.dependency_overrides.clear()


# --- version-safe acceptance -----------------------------------------------------------------


@pytest.mark.parametrize("version", ["2026-10-openai-0", "2026-10-OPENAI-1", "x", "2026-09 "])
def test_an_unknown_version_is_refused_and_changes_nothing(version: str) -> None:
    deployment = App("s5d_unknown", openai=True)
    with TestClient(deployment.app()) as client:
        workspace_id = _workspace(client)
        answer = _accept(client, workspace_id, version)
        assert answer.status_code == 409
        assert answer.json()["error"]["code"] == "consent_notice_unknown"
        assert _record(client, workspace_id).consent_accepted_at is None
    assert deployment.events("consent.accepted") == []


def test_the_digest_binds_acceptance_to_the_notice_that_was_displayed() -> None:
    deployment = App("s5d_digest", openai=True)
    with TestClient(deployment.app()) as client:
        workspace_id = _workspace(client)
        notice = client.get(_url(workspace_id)).json()["notice"]
        for version, digest in (
            (OPENAI_NOTICE_VERSION, "0" * 64),  # a digest the server never issued
            (BASE_NOTICE_VERSION, notice["digest"]),  # the digest of another version
        ):
            refused = _accept(client, workspace_id, version, notice_digest=digest)
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "consent_notice_changed"
        assert _record(client, workspace_id).consent_accepted_at is None
        accepted = _accept(
            client, workspace_id, OPENAI_NOTICE_VERSION, notice_digest=notice["digest"]
        ).json()
        assert accepted["status"] == "current"
        assert accepted["data_recipients"] == ["Gemini", "OpenAI"]


def test_a_notice_displayed_before_a_recipient_was_added_cannot_be_accepted() -> None:
    app = App("s5d_digest_recipients").app()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        notice = client.get(_url(workspace_id)).json()["notice"]
        app.state.settings = replace(
            app.state.settings, llm_providers=("gemini", "anthropic"), anthropic_api_key=FAKE_KEY
        )
        refused = _accept(client, workspace_id, BASE_NOTICE_VERSION, notice_digest=notice["digest"])
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "consent_notice_changed"


@pytest.mark.parametrize(
    "extra",
    [{"recipients": ["OpenAI"]}, {"data_recipients": ["OpenAI"]}, {"status": "current"}],
)
def test_the_browser_cannot_supply_recipients_or_status(extra: dict[str, Any]) -> None:
    with TestClient(App("s5d_escalation", openai=True).app()) as client:
        workspace_id = _workspace(client)
        assert _accept(client, workspace_id, BASE_NOTICE_VERSION, **extra).status_code == 422
        assert _record(client, workspace_id).consent_recipients == ()


def test_older_consent_stays_valid_only_for_what_it_disclosed() -> None:
    with TestClient(App("s5d_stale", openai=True).app()) as client:
        workspace_id = _workspace(client)
        body = _accept(client, workspace_id, BASE_NOTICE_VERSION).json()
        assert (body["status"], body["accepted"]) == ("stale", True)
        assert body["data_recipients"] == ["Gemini"] and body["questions_enabled"] is True
        assert body["notice"]["version"] == OPENAI_NOTICE_VERSION  # the notice still to accept
        snapshot = _consent(_record(client, workspace_id))
        assert snapshot.satisfies({"Gemini"}) and not snapshot.satisfies({"OpenAI"})

        _tamper(client, workspace_id, consent_recipients=("Gemini", "OpenAI"))
        body = client.get(_url(workspace_id)).json()
        assert body["status"] == "stale" and body["data_recipients"] == ["Gemini"]
        assert not _consent(_record(client, workspace_id)).satisfies({"OpenAI"})


def test_adding_a_recipient_makes_consent_stale_and_stops_questions() -> None:
    app = App("s5d_added").app()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        assert _accept_current(client, workspace_id).json()["status"] == "current"
        app.state.settings = replace(
            app.state.settings, llm_providers=("gemini", "anthropic"), anthropic_api_key=FAKE_KEY
        )
        body = client.get(_url(workspace_id)).json()
        assert body["status"] == "stale" and body["questions_enabled"] is False
        assert body["notice"]["recipients"] == ["Gemini", "Anthropic"]


@pytest.mark.parametrize(
    "tampered",
    [
        {"consent_notice_version": "2026-08"},  # a version this server never recognised
        {"consent_notice_version": None},  # recipients without a version
        {"consent_withdrawn_at": T0},  # both accepted and withdrawn is ambiguous
    ],
)
def test_a_tampered_or_ambiguous_record_grants_nobody(tampered: dict[str, Any]) -> None:
    with TestClient(App("s5d_tampered", openai=True).app()) as client:
        workspace_id = _workspace(client)
        _accept_current(client, workspace_id)
        _tamper(client, workspace_id, **tampered)
        snapshot = _consent(_record(client, workspace_id))
        assert snapshot.recipients == () and not snapshot.satisfies({"Gemini"})
        body = client.get(_url(workspace_id)).json()
        assert body["status"] == "stale" and body["accepted"] is False
        assert body["data_recipients"] == [] and body["questions_enabled"] is False
        workspace = client.get(f"/api/v1/workspaces/{workspace_id}").json()
        assert workspace["consent_accepted"] is False


def test_a_tampered_record_stops_model_backed_questions() -> None:
    deployment = App("s5d_tampered_ask")
    with TestClient(deployment.app()) as client:
        workspace_id, conversation_id = _prepare_hybrid_context(client)
        _tamper(client, workspace_id, consent_notice_version="2026-08")
        calls = deployment.llm.calls
        answer = client.post(
            f"/api/v1/conversations/{conversation_id}/messages", json={"question": "Top merchants?"}
        )
        assert answer.status_code == 409
        assert answer.json()["error"]["code"] == "gemini_consent_required"
        assert deployment.llm.calls == calls


def test_accepting_activates_nothing() -> None:
    assert providers_module.LIVE_ADMISSION_RELEASED is False
    assert dict(providers_module.REVIEWED_PROVIDER_EVIDENCE) == {}
    deployment = App("s5d_no_activation", openai=True)
    app = deployment.app()
    with TestClient(app) as client:
        before = app.state.investigation_provider_decision
        assert before is not providers_module.AdmissionDecision.ADMITTED
        workspace_id = _workspace(client)
        assert _accept_current(client, workspace_id).json()["status"] == "current"
        assert app.state.investigation_provider_decision is before
        assert type(app.state.investigations._adapters) is FailClosedAdapters


# --- withdrawal ------------------------------------------------------------------------------


def test_withdrawal_is_durable_idempotent_and_audited_once() -> None:
    deployment = App("s5d_withdraw", openai=True)
    with TestClient(deployment.app()) as client:
        workspace_id = _workspace(client)
        _accept_current(client, workspace_id)
        first = client.delete(_url(workspace_id))
        assert first.status_code == 200
        body = first.json()
        assert (body["status"], body["accepted"]) == ("withdrawn", False)
        assert body["data_recipients"] == [] and body["questions_enabled"] is False
        assert body["accepted_at"] is None and body["withdrawn_at"] is not None
        assert body["notice"]["version"] == OPENAI_NOTICE_VERSION
        second = client.delete(_url(workspace_id)).json()
        assert second["withdrawn_at"] == body["withdrawn_at"]
        record = _record(client, workspace_id)
        assert record.consent_notice_version is None and record.consent_recipients == ()
        assert not _consent(record).satisfies({"Gemini"})
        assert client.get(f"/api/v1/workspaces/{workspace_id}").json()["consent_accepted"] is False
    assert len(deployment.events("consent.withdrawn")) == 1

    with TestClient(deployment.app()) as client:  # a restart reads the durable record
        again = client.get(_url(workspace_id)).json()
        assert again["status"] == "withdrawn"
        assert again["withdrawn_at"] == body["withdrawn_at"]


def test_withdrawing_with_nothing_on_record_changes_nothing() -> None:
    deployment = App("s5d_withdraw_nothing")
    with TestClient(deployment.app()) as client:
        workspace_id = _workspace(client)
        body = client.delete(_url(workspace_id)).json()
        assert body["status"] == "missing" and body["withdrawn_at"] is None
    assert deployment.events("consent.withdrawn") == []


def test_a_new_acceptance_after_withdrawal_is_needed_and_clears_it() -> None:
    with TestClient(App("s5d_again", openai=True).app()) as client:
        workspace_id = _workspace(client)
        _accept_current(client, workspace_id)
        client.delete(_url(workspace_id))
        body = _accept_current(client, workspace_id).json()
        assert (body["status"], body["withdrawn_at"]) == ("current", None)
        assert _consent(_record(client, workspace_id)).satisfies({"OpenAI"})


def test_withdrawal_stops_questions_and_pending_sql_approval_without_a_model_call() -> None:
    """Before S5d an approval never re-read consent, so it resumed model-backed work."""
    deployment = App("s5d_questions")
    with TestClient(deployment.app()) as client:
        workspace_id, conversation_id = _prepare_hybrid_context(client)
        pending = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "question": "Which uploaded transactions violate the policy?",
                "require_sql_approval": True,
            },
        ).json()
        assert pending["status"] == "pending_approval"
        assert client.delete(_url(workspace_id)).status_code == 200
        calls = deployment.llm.calls

        approved = client.post(
            f"/api/v1/messages/{pending['id']}/approval", json={"decision": "approve"}
        )
        assert approved.status_code == 409
        assert approved.json()["error"]["code"] == "gemini_consent_required"
        assert deployment.llm.calls == calls
        assert deployment.events("agent.sql_approved") == []
        messages = client.get(f"/api/v1/conversations/{conversation_id}/messages").json()[
            "messages"
        ]
        assert [message["status"] for message in messages] == ["pending_approval"]

        rejected = client.post(
            f"/api/v1/messages/{pending['id']}/approval", json={"decision": "reject"}
        )
        assert rejected.status_code == 200  # refusing to run the SQL still works
        asked = client.post(
            f"/api/v1/conversations/{conversation_id}/messages", json={"question": "Top merchants?"}
        )
        assert asked.status_code == 409
        assert asked.json()["error"]["code"] == "gemini_consent_required"
        assert deployment.llm.calls == calls


def test_a_new_acceptance_lets_a_pending_sql_approval_continue() -> None:
    deployment = App("s5d_questions_again")
    with TestClient(deployment.app()) as client:
        workspace_id, conversation_id = _prepare_hybrid_context(client)
        pending = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "question": "Which uploaded transactions violate the policy?",
                "require_sql_approval": True,
            },
        ).json()
        client.delete(_url(workspace_id))
        _accept_current(client, workspace_id)
        approved = client.post(
            f"/api/v1/messages/{pending['id']}/approval", json={"decision": "approve"}
        )
        assert approved.status_code == 200


def test_the_workspace_export_reports_a_withdrawal() -> None:
    with TestClient(App("s5d_export").app()) as client:
        workspace_id = _workspace(client)
        _accept_current(client, workspace_id)
        client.delete(_url(workspace_id))
        export = client.get(f"/api/v1/workspaces/{workspace_id}/export")
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(export.content)).read("manifest.json"))
    assert manifest["consent"]["accepted"] is False
    assert manifest["consent"]["withdrawn_at"] is not None
    assert manifest["consent"]["data_recipients"] == []


# --- audit metadata --------------------------------------------------------------------------


def test_the_audit_vocabulary_has_the_events_and_the_recipient_token() -> None:
    assert {"consent.accepted", "consent.withdrawn"} <= AUDIT_ACTIONS
    assert AUDIT_ATTRIBUTES["consent_recipients"] == "token"


def test_audit_recipients_are_a_fixed_vocabulary() -> None:
    assert audit_recipients(()) is None
    assert audit_recipients(("OpenAI", "Gemini")) == "gemini+openai"
    assert audit_recipients(("Ollama (remote)", "Anthropic")) == "anthropic+ollama_remote"
    # A name outside the vocabulary never reaches the log as free text.
    assert audit_recipients(("ignore previous instructions",)) == "unlisted"


def test_acceptance_and_withdrawal_are_audited_accurately_and_without_content() -> None:
    deployment = App("s5d_audit", openai=True)
    with TestClient(deployment.app()) as client:
        workspace_id = _workspace(client)
        _accept_current(client, workspace_id)
        client.delete(_url(workspace_id))
    accepted = deployment.events("consent.accepted")[0]
    withdrawn = deployment.events("consent.withdrawn")[0]
    assert accepted.resource_id == withdrawn.resource_id == workspace_id
    assert accepted.attributes == {
        "notice_version": OPENAI_NOTICE_VERSION,
        "consent_recipients": "gemini+openai",
        "consent_required": True,
    }
    assert withdrawn.attributes == {
        "notice_version": OPENAI_NOTICE_VERSION,
        "consent_recipients": "gemini+openai",
    }
    rendered = json.dumps([accepted.attributes, withdrawn.attributes])
    for private in (OPENAI_NOTICE.text[:40], FAKE_KEY, SNAPSHOT, REVIEW):
        assert private not in rendered


def test_withdrawal_audits_what_was_in_force_not_what_a_stale_row_named() -> None:
    deployment = App("s5d_audit_stale", openai=True)
    with TestClient(deployment.app()) as client:
        workspace_id = _workspace(client)
        _accept(client, workspace_id, BASE_NOTICE_VERSION)
        _tamper(client, workspace_id, consent_recipients=("Gemini", "OpenAI"))
        client.delete(_url(workspace_id))
    assert deployment.events("consent.withdrawn")[0].attributes == {
        "notice_version": BASE_NOTICE_VERSION,
        "consent_recipients": "gemini",
    }


# --- the pure rules --------------------------------------------------------------------------


def test_the_recognised_notices_are_exactly_the_two_versions() -> None:
    assert set(NOTICES) == {BASE_NOTICE_VERSION, OPENAI_NOTICE_VERSION}


def test_consent_in_force_needs_a_recognised_unwithdrawn_acceptance() -> None:
    assert consent_in_force(BASE_NOTICE_VERSION, T0, None)
    for version, accepted, withdrawn in (
        (BASE_NOTICE_VERSION, None, None),
        (BASE_NOTICE_VERSION, T0, T0),
        ("2026-08", T0, None),
        (None, T0, None),
    ):
        assert not consent_in_force(version, accepted, withdrawn)
        assert effective_recipients(version, accepted, withdrawn, ("Gemini",)) == ()


@pytest.mark.parametrize(
    ("required", "effective", "accepted", "withdrawn", "status"),
    [
        ((), (), None, None, "not_required"),
        ((), (), None, T0, "not_required"),
        (("Gemini",), (), None, None, "missing"),
        (("Gemini",), (), None, T0, "withdrawn"),
        (("Gemini",), ("Gemini",), T0, None, "current"),
        (("Gemini", "OpenAI"), ("Gemini",), T0, None, "stale"),
        (("Gemini",), (), T0, T0, "stale"),
    ],
)
def test_consent_status(
    required: tuple[str, ...],
    effective: tuple[str, ...],
    accepted: datetime | None,
    withdrawn: datetime | None,
    status: str,
) -> None:
    assert consent_status(required, effective, accepted, withdrawn) == status


def test_the_digest_changes_with_the_recipients_and_is_stable_otherwise() -> None:
    one = served_notice(("Gemini",))
    two = served_notice(("Gemini", "Anthropic"))
    assert one is not None and two is not None
    assert one.digest != two.digest and one.notice is two.notice
    assert one.digest == served_notice(("Gemini",)).digest  # type: ignore[union-attr]
    assert served_notice(()) is None


# --- migration compatibility -----------------------------------------------------------------


def test_a_v4_database_upgrades_with_existing_consent_intact(tmp_path: Path) -> None:
    path = tmp_path / "metadata.db"
    old = MetadataStore(path, migrations=MIGRATIONS[:4])
    assert old.open().schema_version == 4
    connection = old._connection
    assert connection is not None
    with connection:
        connection.execute(
            "INSERT INTO workspaces (id, tenant_id, authentication_mode, expires_at, "
            "gemini_configured, local_only_mode, consent_notice_version, consent_accepted_at, "
            "created_at, data_recipients, consent_recipients) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _stored_workspace().id,
                _stored_workspace().tenant_id,
                "local",
                "2099-01-01T00:00:00+00:00",
                1,
                0,
                BASE_NOTICE_VERSION,
                "2026-10-01T00:00:00+00:00",
                "2026-10-01T00:00:00+00:00",
                "Gemini",
                "Gemini",
            ),
        )
    old.close()

    upgraded = MetadataStore(path)
    result = upgraded.open()
    assert result.migrations_applied == (5,)
    stored = upgraded.load_workspaces()[0]
    assert stored.consent_notice_version == BASE_NOTICE_VERSION
    assert stored.consent_accepted_at is not None and stored.consent_withdrawn_at is None
    assert stored.consent_recipients == ("Gemini",)
    upgraded.save_workspace(replace(stored, consent_withdrawn_at=T0))
    assert upgraded.load_workspaces()[0].consent_withdrawn_at == T0
    upgraded.close()


def test_existing_consent_stays_current_under_the_notice_it_accepted() -> None:
    deployment = App("s5d_existing")
    with TestClient(deployment.app()) as client:
        workspace_id = _workspace(client)
        _accept(client, workspace_id, BASE_NOTICE_VERSION)  # as the previous web client sent it
    with TestClient(deployment.app()) as client:
        assert client.get(_url(workspace_id)).json()["status"] == "current"


# --- runtime: withdrawal against live investigations -----------------------------------------


class WithdrawingFlow(Flow):
    """The S5a flow, withdrawing through the real endpoint instead of an older acceptance."""

    def withdraw(self) -> None:
        answer = self.client.delete(_url(self.workspace_id))
        assert answer.status_code == 200 and answer.json()["status"] == "withdrawn"

    def accept_covering_the_run(self) -> None:
        """A real acceptance, plus the scripted recipient the fixture also adds to the record."""
        assert _accept_current(self.client, self.workspace_id).status_code == 200
        record = _record(self.client, self.workspace_id)
        _tamper(
            self.client,
            self.workspace_id,
            consent_recipients=(*record.consent_recipients, RECIPIENT),
        )
        assert _consent(_record(self.client, self.workspace_id)).satisfies({RECIPIENT})


def test_withdrawal_at_plan_approval_refuses_it_and_every_record_agrees() -> None:
    adapters = TwoWaves()
    d = Deployment("s5d_plan", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = WithdrawingFlow(client, d)
        body = flow.to_plan_approval()
        flow.withdraw()
        refused = _approve(client, body)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "authority_revoked"
        ended = flow.until(*TERMINAL)
        assert (ended["status"], ended["explanation_code"]) == ("failed", "authority_revoked")
        assert adapters.llm.counts == 0 and adapters.llm.sql_prompts == []
    names = [event.name for event in d.audit.events]
    assert "consent.withdrawn" in names and "agent.investigation_approved" not in names


def test_withdrawal_at_a_sql_wave_refuses_the_resume_and_every_record_agrees() -> None:
    adapters = TwoWaves()
    d = Deployment("s5d_wave", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = WithdrawingFlow(client, d)
        flow.to_first_wave()
        sent = (adapters.llm.counts, len(adapters.llm.sql_prompts))
        flow.withdraw()
        refused = _approve_wave(client, flow.id)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "authority_revoked"
        flow.assert_one_consistent_end("cancelled")
        assert flow.body()["explanation_code"] == "authority_revoked"
        assert (adapters.llm.counts, len(adapters.llm.sql_prompts)) == sent
        assert flow.resume_jobs() <= 1
    names = [event.name for event in d.audit.events]
    assert "consent.withdrawn" in names and "agent.sql_approved" not in names


def test_withdrawal_during_active_execution_stops_before_the_next_create() -> None:
    adapters = TwoWaves()
    adapters.llm.count_gate = Gate(nth=2)
    d = Deployment("s5d_active", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = WithdrawingFlow(client, d)
        flow.to_first_wave()
        assert _approve_wave(client, flow.id).status_code == 202
        assert adapters.llm.count_gate.entered.wait(30)
        flow.withdraw()
        adapters.llm.count_gate.release.set()
        body = flow.until(*TERMINAL)
        assert body["status"] in {"failed", "cancelled"}
        assert adapters.llm.counts == 2 and len(adapters.llm.sql_prompts) == 1  # no second create
        assert not flow.live().run_jobs
        assert flow.live().run._serial.acquire(blocking=False)  # S5a's guard is free
        flow.live().run._serial.release()


def test_a_new_acceptance_never_restores_a_stopped_investigation() -> None:
    adapters = TwoWaves()
    d = Deployment("s5d_no_restore", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = WithdrawingFlow(client, d)
        flow.to_first_wave()
        flow.withdraw()
        assert _approve_wave(client, flow.id).status_code == 409
        flow.until(*TERMINAL)
        sent = (adapters.llm.counts, len(adapters.llm.sql_prompts))
        flow.accept_covering_the_run()
        assert flow.body()["status"] == "cancelled"
        assert client.get(f"/api/v1/investigations/{flow.id}/sql-wave").status_code != 200
        assert (adapters.llm.counts, len(adapters.llm.sql_prompts)) == sent


def test_reaccepting_before_any_check_continues_a_paused_run_under_the_new_consent() -> None:
    """Pinned behaviour: the live check reads current consent, so it has no memory of a withdrawal
    no check observed. Continuing needs the explicit new acceptance and the wave's own approval."""
    adapters = TwoWaves()
    d = Deployment("s5d_reaccept", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = WithdrawingFlow(client, d)
        flow.to_first_wave()
        first = flow.wave_id()
        flow.withdraw()
        flow.accept_covering_the_run()
        answer = _approve_wave(client, flow.id)
        assert answer.status_code == 202, answer.json()
        settled = flow.after_resume(first)
        assert settled["status"] not in {"failed", "cancelled"}
        assert len(adapters.llm.sql_prompts) == 2
        assert flow.cancel().status_code in {200, 202}
        flow.until(*TERMINAL)


def test_valid_consent_with_no_withdrawal_still_runs_to_the_next_wave() -> None:
    """The control: the same flow without a withdrawal is not refused."""
    adapters = TwoWaves()
    d = Deployment("s5d_control", adapters=adapters)
    with TestClient(d.app()) as client:
        flow = WithdrawingFlow(client, d)
        flow.to_first_wave()
        first = flow.wave_id()
        assert _approve_wave(client, flow.id).status_code == 202
        settled = flow.after_resume(first)
        assert settled["status"] not in {"failed", "cancelled"}
        assert len(adapters.llm.sql_prompts) == 2
        assert flow.cancel().status_code in {200, 202}
        flow.until(*TERMINAL)
