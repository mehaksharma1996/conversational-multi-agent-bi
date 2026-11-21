"""UI-5 (#153) S1: removing a workspace's indexed documents through the API."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.api.dependencies import get_identity
from apps.api.main import create_app
from packages.connectors import IdentityContext
from packages.governance import InMemoryAuditSink
from src.documents.vector_store import ChromaDocumentStore
from src.utils.identity import LOCAL_DEV_TENANT_ID
from tests.test_api_features import FakeEmbedder, FakeLLM, _pdf_payload, _settings
from tests.test_utils import isolated_directory_path


@pytest.fixture
def app_and_audit() -> Iterator[tuple[TestClient, InMemoryAuditSink, object]]:
    tmp_path = isolated_directory_path("api_document_deletion")
    audit = InMemoryAuditSink()
    app = create_app(
        settings=_settings(tmp_path),
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        audit_sink=audit,
    )
    yield TestClient(app), audit, app
    app.dependency_overrides.clear()


def _index(client: TestClient) -> tuple[str, str]:
    workspace_id = client.post("/api/v1/workspaces").json()["id"]
    documents = client.post(
        f"/api/v1/workspaces/{workspace_id}/document-collections",
        files=[("files", ("policy.pdf", _pdf_payload(), "application/pdf"))],
    )
    assert documents.status_code == 201
    return workspace_id, str(documents.json()["id"])


def _stored_chunks(app: object, workspace_id: str) -> int:
    repository = app.state.repository  # type: ignore[attr-defined]
    store = ChromaDocumentStore(
        repository.workspace_dir(workspace_id, LOCAL_DEV_TENANT_ID) / "vectorstore",
        FakeEmbedder(),
    )
    try:
        return int(store._collection.count())
    finally:
        store.close()


def test_deleting_removes_the_record_the_chunks_and_audits_counts_only(
    app_and_audit: tuple[TestClient, InMemoryAuditSink, object],
) -> None:
    client, audit, app = app_and_audit
    workspace_id, collection_id = _index(client)
    assert _stored_chunks(app, workspace_id) > 0

    response = client.delete(f"/api/v1/document-collections/{collection_id}")

    assert response.status_code == 204 and response.content == b""
    assert client.get(f"/api/v1/document-collections/{collection_id}").status_code == 404
    assert _stored_chunks(app, workspace_id) == 0
    [event] = [e for e in audit.for_tenant(LOCAL_DEV_TENANT_ID) if e.name == "documents.deleted"]
    assert event.resource_id == collection_id
    assert "policy" not in repr(event).lower()  # no filename or text in the audit record


def test_a_repeat_or_unknown_delete_is_not_found(
    app_and_audit: tuple[TestClient, InMemoryAuditSink, object],
) -> None:
    client, _, _ = app_and_audit
    _, collection_id = _index(client)

    assert client.delete(f"/api/v1/document-collections/{collection_id}").status_code == 204
    assert client.delete(f"/api/v1/document-collections/{collection_id}").status_code == 404
    assert client.delete("/api/v1/document-collections/docs_unknown").status_code == 404


def test_another_tenant_cannot_delete_or_detect_the_collection(
    app_and_audit: tuple[TestClient, InMemoryAuditSink, object],
) -> None:
    client, audit, app = app_and_audit
    workspace_id, collection_id = _index(client)
    app.dependency_overrides[get_identity] = lambda: IdentityContext(  # type: ignore[attr-defined]
        tenant_id="tenant-other",
        subject="other",
        authentication_mode="test",
        roles=frozenset({"workspace_admin"}),
    )

    response = client.delete(f"/api/v1/document-collections/{collection_id}")

    app.dependency_overrides.clear()  # type: ignore[attr-defined]
    assert response.status_code == 404
    assert client.get(f"/api/v1/document-collections/{collection_id}").status_code == 200
    assert _stored_chunks(app, workspace_id) > 0
    assert "documents.deleted" not in audit.names("tenant-other")


def test_a_viewer_cannot_delete_documents(
    app_and_audit: tuple[TestClient, InMemoryAuditSink, object],
) -> None:
    client, _, app = app_and_audit
    _, collection_id = _index(client)
    app.dependency_overrides[get_identity] = lambda: IdentityContext(  # type: ignore[attr-defined]
        tenant_id=LOCAL_DEV_TENANT_ID,
        subject="viewer",
        authentication_mode="test",
        roles=frozenset({"viewer"}),
    )

    response = client.delete(f"/api/v1/document-collections/{collection_id}")

    app.dependency_overrides.clear()  # type: ignore[attr-defined]
    assert response.status_code == 403
    assert client.get(f"/api/v1/document-collections/{collection_id}").status_code == 200


def test_a_conversation_bound_to_deleted_documents_can_no_longer_ask(
    app_and_audit: tuple[TestClient, InMemoryAuditSink, object],
) -> None:
    client, _, _ = app_and_audit
    workspace_id, collection_id = _index(client)
    client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )
    conversation = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        json={"document_collection_id": collection_id},
    ).json()

    client.delete(f"/api/v1/document-collections/{collection_id}")
    asked = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"question": "What does the policy require?"},
    )

    assert asked.status_code == 404


def test_the_workspace_can_index_again_after_deletion(
    app_and_audit: tuple[TestClient, InMemoryAuditSink, object],
) -> None:
    client, _, app = app_and_audit
    workspace_id, collection_id = _index(client)
    client.delete(f"/api/v1/document-collections/{collection_id}")

    again = client.post(
        f"/api/v1/workspaces/{workspace_id}/document-collections",
        files=[("files", ("policy.pdf", _pdf_payload(), "application/pdf"))],
    )

    assert again.status_code == 201
    assert _stored_chunks(app, workspace_id) > 0
