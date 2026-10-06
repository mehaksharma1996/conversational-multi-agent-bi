"""Conversations, messages, exports, and the document index survive a restart (ADR 0022, 12c)."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from typing import Any

from fastapi.testclient import TestClient

from tests.test_api_durable_metadata import Deployment, _as_other_tenant
from tests.test_api_features import _pdf_payload

FEATURE_CSV = (
    b"transaction_date,amount,merchant,customer_id\n"
    b"2026-01-01,10,Safe Merchant,C-1\n"
    b"2026-01-02,75,=2+2,C-2\n"
)
MAPPING = {
    "amount": "amount",
    "date": "transaction_date",
    "customer_id": "customer_id",
    "merchant": "merchant",
    "location": None,
    "label": None,
}
HYBRID = "Which uploaded transactions violate the policy?"
RAG = "According to the policy, what needs escalation?"


class Context:
    def __init__(self, client: TestClient) -> None:
        workspace = client.post("/api/v1/workspaces").json()
        self.workspace_id: str = workspace["id"]
        upload = client.post(
            f"/api/v1/workspaces/{self.workspace_id}/tabular-uploads",
            files={"file": ("transactions.csv", FEATURE_CSV, "text/csv")},
        )
        self.dataset_id: str = client.post(
            f"/api/v1/tabular-uploads/{upload.json()['id']}/dataset", json={}
        ).json()["id"]
        assert (
            client.put(
                f"/api/v1/datasets/{self.dataset_id}/schema-mapping", json=MAPPING
            ).status_code
            == 200
        )
        assert (
            client.post(f"/api/v1/datasets/{self.dataset_id}/analyses", json={}).status_code == 201
        )
        documents = client.post(
            f"/api/v1/workspaces/{self.workspace_id}/document-collections",
            files=[("files", ("policy.pdf", _pdf_payload(), "application/pdf"))],
        )
        assert documents.status_code == 201, documents.text
        self.documents_id: str = documents.json()["id"]
        assert client.put(
            f"/api/v1/workspaces/{self.workspace_id}/consent",
            json={"accepted": True, "notice_version": "2026-09"},
        ).json()["accepted"]
        conversation = client.post(
            f"/api/v1/workspaces/{self.workspace_id}/conversations",
            json={"dataset_id": self.dataset_id, "document_collection_id": self.documents_id},
        )
        assert conversation.status_code == 201, conversation.text
        self.conversation_id: str = conversation.json()["id"]


def _ask(client: TestClient, conversation_id: str, question: str, **extra: Any) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages", json={"question": question, **extra}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _messages(client: TestClient, conversation_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert response.status_code == 200
    messages: list[dict[str, Any]] = response.json()["messages"]
    return messages


def _content_db(deployment: Deployment) -> Any:
    return next((deployment.root / "api").glob("*/ws_*/content.db"))


def test_conversation_history_exports_and_the_document_index_survive_a_restart() -> None:
    deployment = Deployment("conv_full", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx = Context(client)
        rag = _ask(client, ctx.conversation_id, RAG)
        hybrid = _ask(client, ctx.conversation_id, HYBRID)
        export = client.post(f"/api/v1/messages/{hybrid['id']}/exports", json={"format": "csv"})
        assert export.status_code == 201
        csv_before = client.get(f"/api/v1/exports/{export.json()['id']}/content").content
        before = _messages(client, ctx.conversation_id)

    with TestClient(deployment.app()) as client:
        started = deployment.started()
        assert started["document_collections_pending"] == 1
        assert started["conversations_pending"] == 1
        assert client.get(f"/api/v1/conversations/{ctx.conversation_id}").status_code == 200
        assert _messages(client, ctx.conversation_id) == before
        assert [m["id"] for m in before] == [rag["id"], hybrid["id"]]
        csv_after = client.get(f"/api/v1/exports/{export.json()['id']}/content")
        assert csv_after.status_code == 200 and csv_after.content == csv_before
        assert b"'=2+2" in csv_after.content, "formula neutralisation must still be present"
        # The index is reopened rather than rebuilt: a new document question is answered from it.
        again = _ask(client, ctx.conversation_id, RAG)
        assert again["route"] == "rag" and again["sources"]
        # Consent, dataset, and documents are all recovered, so a hybrid question still works.
        follow_up = _ask(client, ctx.conversation_id, HYBRID)
        assert follow_up["route"] == "hybrid" and follow_up["rows"][0]["amount"] == 75


def test_only_retained_messages_and_their_exports_survive() -> None:
    deployment = Deployment("conv_retention", sqlite_encryption_key=None)  # keeps 2 messages
    with TestClient(deployment.app()) as client:
        ctx = Context(client)
        first = _ask(client, ctx.conversation_id, HYBRID)
        export = client.post(f"/api/v1/messages/{first['id']}/exports", json={"format": "csv"})
        second = _ask(client, ctx.conversation_id, HYBRID + " again")
        third = _ask(client, ctx.conversation_id, HYBRID + " once more")
        assert client.get(f"/api/v1/exports/{export.json()['id']}/content").status_code == 404

    with TestClient(deployment.app()) as client:
        assert [m["id"] for m in _messages(client, ctx.conversation_id)] == [
            second["id"],
            third["id"],
        ]
        assert client.get(f"/api/v1/exports/{export.json()['id']}/content").status_code == 404
        rows = [m["rows"] is not None for m in _messages(client, ctx.conversation_id)]
        assert rows == [False, True], "only the newest result frame is retained, as before"

    with closing(sqlite3.connect(_content_db(deployment))) as connection:
        ids = {row[0] for row in connection.execute("SELECT id FROM messages")}
        assert ids == {second["id"], third["id"]}, "dropped messages must leave the database"
        assert connection.execute("SELECT COUNT(*) FROM exports").fetchone() == (0,)


def test_a_pending_sql_approval_is_failed_safely_after_a_restart() -> None:
    deployment = Deployment("conv_approval", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx = Context(client)
        pending = _ask(client, ctx.conversation_id, HYBRID, require_sql_approval=True)
        assert pending["status"] == "pending_approval"

    with TestClient(deployment.app()) as client:
        (message,) = _messages(client, ctx.conversation_id)
        assert message["status"] == "rejected" and message["rows"] is None
        assert "cancelled because the service restarted" in message["answer"]
        # The conversation is usable again: no approval is pending, so a new question is allowed.
        assert _ask(client, ctx.conversation_id, HYBRID)["status"] == "complete"


def test_recovered_conversation_content_never_crosses_tenants() -> None:
    deployment = Deployment("conv_tenants", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx = Context(client)
        hybrid = _ask(client, ctx.conversation_id, HYBRID)
        export = client.post(f"/api/v1/messages/{hybrid['id']}/exports", json={"format": "csv"})

    app = deployment.app()
    with TestClient(app) as client:
        _as_other_tenant(app)
        for path in (
            f"/api/v1/conversations/{ctx.conversation_id}",
            f"/api/v1/conversations/{ctx.conversation_id}/messages",
            f"/api/v1/exports/{export.json()['id']}/content",
            f"/api/v1/document-collections/{ctx.documents_id}",
        ):
            assert client.get(path).status_code == 404, path
        app.dependency_overrides.clear()
        assert client.get(f"/api/v1/conversations/{ctx.conversation_id}").status_code == 200


def test_content_is_encrypted_at_rest_when_a_key_is_configured_and_recovers_with_it() -> None:
    key = bytes(range(32))
    deployment = Deployment("conv_encrypted", sqlite_encryption_key=key)
    with TestClient(deployment.app()) as client:
        ctx = Context(client)
        _ask(client, ctx.conversation_id, HYBRID)
    database = _content_db(deployment)
    assert HYBRID.encode() not in database.read_bytes()
    with closing(sqlite3.connect(database)) as plain:
        try:
            plain.execute("SELECT * FROM sqlite_master").fetchall()
            raise AssertionError("an encrypted content database must not open as plain SQLite")
        except sqlite3.DatabaseError:
            pass

    with TestClient(deployment.app()) as client:
        assert len(_messages(client, ctx.conversation_id)) == 1

    wrong = Deployment("conv_encrypted", sqlite_encryption_key=bytes(32))
    wrong.root = deployment.root
    with TestClient(wrong.app()) as client:
        # The service stays up; content that cannot be decrypted is simply not recovered.
        assert client.get(f"/api/v1/conversations/{ctx.conversation_id}").status_code == 404
        assert client.get(f"/api/v1/workspaces/{ctx.workspace_id}").status_code == 200


def test_a_missing_or_partial_document_index_is_dropped_with_what_depends_on_it() -> None:
    deployment = Deployment("conv_index", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx = Context(client)
        _ask(client, ctx.conversation_id, HYBRID)
    for index_dir in (deployment.root / "api").glob("*/ws_*/vectorstore"):
        shutil.rmtree(index_dir)

    with TestClient(deployment.app()) as client:
        assert client.get(f"/api/v1/document-collections/{ctx.documents_id}").status_code == 404
        assert client.get(f"/api/v1/conversations/{ctx.conversation_id}").status_code == 404
        assert client.get(f"/api/v1/datasets/{ctx.dataset_id}").status_code == 200
        assert client.get(f"/api/v1/workspaces/{ctx.workspace_id}").status_code == 200

    with closing(sqlite3.connect(deployment.metadata_db)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM document_collections").fetchone() == (0,)


def test_deleting_a_workspace_after_a_restart_removes_its_content_database() -> None:
    deployment = Deployment("conv_delete", sqlite_encryption_key=None)
    with TestClient(deployment.app()) as client:
        ctx = Context(client)
        _ask(client, ctx.conversation_id, HYBRID)
    database = _content_db(deployment)

    with TestClient(deployment.app()) as client:
        assert client.delete(f"/api/v1/workspaces/{ctx.workspace_id}").status_code == 204
        assert client.get(f"/api/v1/conversations/{ctx.conversation_id}").status_code == 404

    assert not database.exists()
    with TestClient(deployment.app()):
        assert deployment.started()["conversations_pending"] == 0
