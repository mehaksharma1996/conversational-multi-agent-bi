"""Feature-parity API tests for documents, chat, exports, reports, and reset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from reportlab.pdfgen import canvas

from apps.api.dependencies import get_identity
from apps.api.errors import ResourceNotFoundError
from apps.api.main import create_app
from apps.api.repository import LocalResourceRepository
from config.settings import Settings
from packages.connectors import IdentityContext
from packages.governance import InMemoryAuditSink
from src.llm.base import LLMResponse
from src.utils.identity import LOCAL_DEV_TENANT_ID
from tests.test_utils import isolated_directory_path


class FakeEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [
            [
                1.0 if "policy" in text.lower() else 0.5,
                1.0 if "escalat" in text.lower() else 0.25,
                float(len(text)) / 1_000,
            ]
            for text in texts
        ]


class FakeLLM:
    provider = "fake"
    model = "fake-model"
    configured = True

    def generate(self, prompt: str) -> LLMResponse:
        if "Classify a business-intelligence question" in prompt:
            if "Which uploaded transactions" in prompt:
                text = '{"route":"hybrid","confidence":1}'
            elif "policy" in prompt.lower():
                text = '{"route":"rag","confidence":1}'
            else:
                text = '{"route":"sql","confidence":1}'
        elif "Extract business criteria" in prompt:
            text = '{"thresholds":[50]}'
        elif "careful document analyst" in prompt:
            text = 'The policy states "Transactions over 50 require escalation." Source 1'
        else:
            text = (
                'SELECT "merchant", "amount" FROM "uploaded_data" '
                'WHERE "amount" > 50 ORDER BY "amount" DESC'
            )
        return LLMResponse(text=text, model=self.model, provider=self.provider)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_data_dir=tmp_path,
        sqlite_db_path=tmp_path / "sqlite" / "app.db",
        chroma_persist_dir=tmp_path / "vectorstore",
        gemini_api_key="test-api-key",
        gemini_model="fake-model",
        embedding_model="fake-embedder",
        max_tabular_upload_bytes=10_000,
        max_tabular_rows=100,
        max_pdf_upload_bytes=10_000,
        max_total_pdf_bytes=20_000,
        max_pdf_pages=10,
        max_document_chunks=100,
        retrieval_max_distance=None,
        max_chat_messages=2,
        max_chat_dataframes_retained=1,
    )


def _pdf_payload() -> bytes:
    buffer = BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 720, "Policy: Transactions over 50 require escalation.")
    document.save()
    return buffer.getvalue()


def _prepare_tabular_context(client: TestClient, workspace_id: str) -> tuple[str, str]:
    csv_payload = (
        b"transaction_date,amount,merchant,customer_id\n"
        b"2026-01-01,10,Safe Merchant,C-1\n"
        b"2026-01-02,75,=2+2,C-2\n"
    )
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
        files={"file": ("transactions.csv", csv_payload, "text/csv")},
    )
    dataset = client.post(f"/api/v1/tabular-uploads/{upload.json()['id']}/dataset", json={})
    dataset_id = dataset.json()["id"]
    mapping = {
        "amount": "amount",
        "date": "transaction_date",
        "customer_id": "customer_id",
        "merchant": "merchant",
        "location": None,
        "label": None,
    }
    confirmed = client.put(
        f"/api/v1/datasets/{dataset_id}/schema-mapping",
        json=mapping,
    )
    assert confirmed.status_code == 200
    analysis = client.post(f"/api/v1/datasets/{dataset_id}/analyses", json={})
    assert analysis.status_code == 201
    return dataset_id, str(analysis.json()["id"])


def _prepare_hybrid_context(client: TestClient) -> tuple[str, str]:
    workspace_id = client.post("/api/v1/workspaces").json()["id"]
    dataset_id, _ = _prepare_tabular_context(client, workspace_id)
    documents = client.post(
        f"/api/v1/workspaces/{workspace_id}/document-collections",
        files=[("files", ("policy.pdf", _pdf_payload(), "application/pdf"))],
    )
    assert documents.status_code == 201
    consent = client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )
    assert consent.status_code == 200
    conversation = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        json={
            "dataset_id": dataset_id,
            "document_collection_id": documents.json()["id"],
        },
    )
    assert conversation.status_code == 201
    return workspace_id, str(conversation.json()["id"])


def test_pdf_chat_hybrid_export_report_and_reset() -> None:
    tmp_path = isolated_directory_path("api_feature_parity")
    app = create_app(
        settings=_settings(tmp_path),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
    )
    client = TestClient(app)
    workspace = client.post("/api/v1/workspaces").json()
    workspace_id = workspace["id"]
    assert workspace["consent_required"] is True
    assert workspace["consent_accepted"] is False
    dataset_id, analysis_id = _prepare_tabular_context(client, workspace_id)

    documents = client.post(
        f"/api/v1/workspaces/{workspace_id}/document-collections",
        files=[("files", ("policy.pdf", _pdf_payload(), "application/pdf"))],
    )
    assert documents.status_code == 201
    assert documents.json()["document_count"] == 1
    assert documents.json()["chunk_count"] >= 1

    conversation = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        json={
            "dataset_id": dataset_id,
            "document_collection_id": documents.json()["id"],
        },
    )
    assert conversation.status_code == 201
    conversation_id = conversation.json()["id"]

    blocked = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": "According to the policy, what needs escalation?"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "gemini_consent_required"

    consent = client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )
    assert consent.status_code == 200
    assert consent.json()["accepted"] is True

    rag_message = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": "According to the policy, what needs escalation?"},
    )
    assert rag_message.status_code == 201
    assert rag_message.json()["route"] == "rag"
    assert rag_message.json()["sources"]

    hybrid_message = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": "Which uploaded transactions violate the policy?"},
    )
    assert hybrid_message.status_code == 201
    assert hybrid_message.json()["route"] == "hybrid"
    assert hybrid_message.json()["status"] == "complete"
    assert hybrid_message.json()["sql"].startswith("SELECT")
    assert hybrid_message.json()["rows"][0]["amount"] == 75

    export = client.post(
        f"/api/v1/messages/{hybrid_message.json()['id']}/exports",
        json={"format": "csv"},
    )
    assert export.status_code == 201
    downloaded_export = client.get(f"/api/v1/exports/{export.json()['id']}/content")
    assert downloaded_export.status_code == 200
    assert "'=2+2" in downloaded_export.text

    excel_export = client.post(
        f"/api/v1/messages/{hybrid_message.json()['id']}/exports",
        json={"format": "xlsx"},
    )
    downloaded_excel = client.get(f"/api/v1/exports/{excel_export.json()['id']}/content")
    assert excel_export.status_code == 201
    assert downloaded_excel.status_code == 200
    workbook = load_workbook(BytesIO(downloaded_excel.content), read_only=True)
    assert workbook.active["A2"].value == "'=2+2"

    report = client.post(
        f"/api/v1/analyses/{analysis_id}/reports",
        json={"include_charts": False},
    )
    assert report.status_code == 201
    markdown = client.get(
        f"/api/v1/reports/{report.json()['id']}/content",
        params={"format": "markdown"},
    )
    pdf = client.get(
        f"/api/v1/reports/{report.json()['id']}/content",
        params={"format": "pdf"},
    )
    assert markdown.status_code == 200
    assert "# Business Intelligence Analysis Report" in markdown.text
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")

    second_hybrid = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": "Which uploaded transactions violate the policy again?"},
    )
    assert second_hybrid.status_code == 201
    assert second_hybrid.json()["route"] == "hybrid"

    messages = client.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert messages.status_code == 200
    assert len(messages.json()["messages"]) == 2
    assert [message["id"] for message in messages.json()["messages"]] == [
        hybrid_message.json()["id"],
        second_hybrid.json()["id"],
    ]
    expired_frame = client.post(
        f"/api/v1/messages/{hybrid_message.json()['id']}/exports",
        json={"format": "csv"},
    )
    assert expired_frame.status_code == 409
    assert expired_frame.json()["error"]["code"] == "message_has_no_tabular_result"

    app.dependency_overrides[get_identity] = lambda: IdentityContext(
        tenant_id="b" * 32,
        subject="other-user",
        authentication_mode="test",
        roles=frozenset({"workspace_admin"}),
    )
    try:
        isolated_responses = [
            client.get(f"/api/v1/document-collections/{documents.json()['id']}"),
            client.get(f"/api/v1/conversations/{conversation_id}"),
            client.get(f"/api/v1/conversations/{conversation_id}/messages"),
            client.get(f"/api/v1/reports/{report.json()['id']}/content"),
            client.get(f"/api/v1/exports/{export.json()['id']}/content"),
            client.get(f"/api/v1/exports/{excel_export.json()['id']}/content"),
            client.put(
                f"/api/v1/workspaces/{workspace_id}/consent",
                json={"accepted": True, "notice_version": "2026-09"},
            ),
            client.delete(f"/api/v1/workspaces/{workspace_id}"),
        ]
    finally:
        app.dependency_overrides.clear()

    assert all(response.status_code == 404 for response in isolated_responses)
    assert all(
        response.json()["error"]["code"] == "resource_not_found" for response in isolated_responses
    )

    workspace_dir = app.state.repository.workspace_dir(workspace_id, LOCAL_DEV_TENANT_ID)
    deleted = client.delete(f"/api/v1/workspaces/{workspace_id}")
    assert deleted.status_code == 204
    assert not workspace_dir.exists()
    assert client.get(f"/api/v1/conversations/{conversation_id}").status_code == 404


def test_sql_approval_api_is_tenant_scoped_revalidated_and_audited() -> None:
    tmp_path = isolated_directory_path("api_sql_approval")
    audit_sink = InMemoryAuditSink()
    app = create_app(
        settings=_settings(tmp_path),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        audit_sink=audit_sink,
    )
    client = TestClient(app)
    workspace_id, conversation_id = _prepare_hybrid_context(client)

    pending = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "question": "Which uploaded transactions violate the policy?",
            "require_sql_approval": True,
        },
    )
    assert pending.status_code == 201
    assert pending.json()["status"] == "pending_approval"
    assert pending.json()["rows"] is None
    assert pending.json()["sql"].startswith("SELECT")
    message_id = pending.json()["id"]
    assert app.state.approval_checkpoints.pending_count(workspace_id) == 1
    second_question = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": "What else matches?"},
    )
    assert second_question.status_code == 409
    assert second_question.json()["error"]["code"] == "sql_approval_already_pending"

    app.dependency_overrides[get_identity] = lambda: IdentityContext(
        tenant_id="b" * 32,
        subject="other-user",
        authentication_mode="test",
        roles=frozenset({"workspace_admin"}),
    )
    try:
        cross_tenant = client.post(
            f"/api/v1/messages/{message_id}/approval",
            json={"decision": "approve"},
        )
    finally:
        app.dependency_overrides.clear()
    assert cross_tenant.status_code == 404

    approved = client.post(
        f"/api/v1/messages/{message_id}/approval",
        json={"decision": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "complete"
    assert approved.json()["rows"][0]["amount"] == 75
    duplicate = client.post(
        f"/api/v1/messages/{message_id}/approval",
        json={"decision": "approve"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "sql_approval_not_pending"

    safe_edit_pending = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "question": "Which uploaded transactions violate the policy?",
            "require_sql_approval": True,
        },
    )
    safe_edit = client.post(
        f"/api/v1/messages/{safe_edit_pending.json()['id']}/approval",
        json={
            "decision": "approve",
            "sql": 'SELECT "merchant" FROM "uploaded_data" ORDER BY "merchant"',
        },
    )
    assert safe_edit.status_code == 200
    assert safe_edit.json()["status"] == "complete"
    assert safe_edit.json()["rows"] == [{"merchant": "=2+2"}, {"merchant": "Safe Merchant"}]

    reject_pending = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "question": "Which uploaded transactions violate the policy?",
            "require_sql_approval": True,
        },
    )
    rejected = client.post(
        f"/api/v1/messages/{reject_pending.json()['id']}/approval",
        json={"decision": "reject"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["rows"] is None

    unsafe_pending = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "question": "Which uploaded transactions violate the policy?",
            "require_sql_approval": True,
        },
    )
    unsafe = client.post(
        f"/api/v1/messages/{unsafe_pending.json()['id']}/approval",
        json={"decision": "approve", "sql": "DROP TABLE uploaded_data"},
    )
    assert unsafe.status_code == 422
    assert unsafe.json()["error"]["code"] == "sql_approval_could_not_be_applied_safely"
    failed_retry = client.post(
        f"/api/v1/messages/{unsafe_pending.json()['id']}/approval",
        json={"decision": "reject"},
    )
    assert failed_retry.status_code == 409
    rejected_export = client.post(
        f"/api/v1/messages/{unsafe_pending.json()['id']}/exports",
        json={"format": "csv"},
    )
    assert rejected_export.status_code == 409
    assert rejected_export.json()["error"]["code"] == "message_not_complete"
    assert app.state.approval_checkpoints.pending_count(workspace_id) == 0

    approval_events = [
        event
        for event in audit_sink.events
        if event.name in {"agent.sql_approved", "agent.sql_rejected"}
    ]
    assert [event.name for event in approval_events] == [
        "agent.sql_approved",
        "agent.sql_approved",
        "agent.sql_rejected",
        "agent.sql_approved",
    ]
    assert all("sql" not in event.attributes for event in approval_events)
    assert approval_events[3].attributes["outcome"] == "failure"
    assert approval_events[3].attributes["sql_edited"] is True


def test_workspace_deletion_removes_pending_sql_checkpoint() -> None:
    tmp_path = isolated_directory_path("api_sql_approval_cleanup")
    app = create_app(
        settings=_settings(tmp_path),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
    )
    client = TestClient(app)
    workspace_id, conversation_id = _prepare_hybrid_context(client)
    pending = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "question": "Which uploaded transactions violate the policy?",
            "require_sql_approval": True,
        },
    )
    assert pending.json()["status"] == "pending_approval"
    assert app.state.approval_checkpoints.pending_count(workspace_id) == 1

    deleted = client.delete(f"/api/v1/workspaces/{workspace_id}")

    assert deleted.status_code == 204
    assert app.state.approval_checkpoints.pending_count(workspace_id) == 0


def test_workspace_expiry_removes_pending_sql_checkpoint() -> None:
    tmp_path = isolated_directory_path("api_sql_approval_expiry")
    app = create_app(
        settings=_settings(tmp_path),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
    )
    client = TestClient(app)
    workspace_id, conversation_id = _prepare_hybrid_context(client)
    expires_at = datetime.fromisoformat(
        client.get(f"/api/v1/workspaces/{workspace_id}").json()["expires_at"]
    )
    pending = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "question": "Which uploaded transactions violate the policy?",
            "require_sql_approval": True,
        },
    )
    assert pending.json()["status"] == "pending_approval"
    assert app.state.approval_checkpoints.pending_count(workspace_id) == 1

    with patch(
        "apps.api.repository._now",
        return_value=expires_at + timedelta(seconds=1),
    ):
        expired = client.get(f"/api/v1/workspaces/{workspace_id}")

    assert expired.status_code == 404
    assert app.state.approval_checkpoints.pending_count(workspace_id) == 0


def test_document_upload_rejects_non_pdf_before_indexing() -> None:
    tmp_path = isolated_directory_path("api_feature_invalid_pdf")
    app = create_app(
        settings=_settings(tmp_path),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
    )
    client = TestClient(app)
    workspace_id = client.post("/api/v1/workspaces").json()["id"]

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/document-collections",
        files=[("files", ("notes.txt", b"not a PDF", "text/plain"))],
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_document_type"


def test_expired_workspace_removes_child_resources_and_local_storage() -> None:
    tmp_path = isolated_directory_path("api_feature_retention")
    repository = LocalResourceRepository(storage_root=tmp_path, retention_hours=1)
    started_at = datetime(2026, 9, 29, 12, tzinfo=UTC)
    with patch("apps.api.repository._now", return_value=started_at):
        workspace = repository.create_workspace(
            tenant_id=LOCAL_DEV_TENANT_ID,
            authentication_mode="local",
            idempotency_key="retention-test",
        )
        upload = repository.create_upload(
            workspace.id,
            LOCAL_DEV_TENANT_ID,
            "transactions.csv",
            "text/csv",
            b"amount\n10\n",
        )
        workspace_dir = repository.workspace_dir(workspace.id, LOCAL_DEV_TENANT_ID)

    with patch(
        "apps.api.repository._now",
        return_value=started_at + timedelta(hours=2),
    ):
        with pytest.raises(ResourceNotFoundError):
            repository.get_upload(upload.id, LOCAL_DEV_TENANT_ID)

    assert not workspace_dir.exists()
    with pytest.raises(ResourceNotFoundError):
        repository.get_workspace(workspace.id, LOCAL_DEV_TENANT_ID)
