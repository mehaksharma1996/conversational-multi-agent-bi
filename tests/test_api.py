"""HTTP contract and ownership tests for the FastAPI tabular vertical slice."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.dependencies import get_identity
from apps.api.main import create_app
from apps.api.repository import LocalResourceRepository
from config.settings import Settings
from packages.connectors import IdentityContext
from tests.test_utils import isolated_directory_path


def _settings(tmp_path: Path, **overrides: int) -> Settings:
    values = {
        "max_tabular_upload_bytes": 10_000,
        "max_tabular_rows": 100,
    }
    values.update(overrides)
    return Settings(
        app_data_dir=tmp_path,
        sqlite_db_path=tmp_path / "sqlite" / "app.db",
        chroma_persist_dir=tmp_path / "vectorstore",
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash",
        embedding_model="all-MiniLM-L6-v2",
        max_tabular_upload_bytes=values["max_tabular_upload_bytes"],
        max_tabular_rows=values["max_tabular_rows"],
    )


@pytest.fixture
def api_app() -> FastAPI:
    return create_app(settings=_settings(isolated_directory_path("api_app")))


@pytest.fixture
def client(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


def _csv_payload(row_count: int = 10) -> bytes:
    rows = ["transaction_date,amount,merchant,customer_id"]
    rows.extend(
        f"2026-01-{index + 1:02d},{999 if index == row_count - 1 else 10 + index},"
        f"Merchant {index % 2},customer-{index % 3}"
        for index in range(row_count)
    )
    return ("\n".join(rows) + "\n").encode()


def _create_workspace(client: TestClient) -> str:
    response = client.post("/api/v1/workspaces")
    assert response.status_code == 201
    return str(response.json()["id"])


def _upload_csv(client: TestClient, workspace_id: str) -> str:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
        files={"file": ("transactions.csv", _csv_payload(), "text/csv")},
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _create_dataset(client: TestClient, upload_id: str) -> str:
    response = client.post(f"/api/v1/tabular-uploads/{upload_id}/dataset", json={})
    assert response.status_code == 201
    return str(response.json()["id"])


def _schema_mapping() -> dict[str, str | None]:
    return {
        "amount": "amount",
        "date": "transaction_date",
        "customer_id": "customer_id",
        "merchant": "merchant",
        "location": None,
        "label": None,
    }


def test_health_checks_and_server_generated_request_id(client: TestClient) -> None:
    live = client.get("/health/live", headers={"X-Request-ID": "client-controlled"})
    ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert live.headers["X-Request-ID"] != "client-controlled"
    assert len(live.headers["X-Request-ID"]) == 32
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_workspace_creation_is_idempotent_for_tenant_and_key(client: TestClient) -> None:
    headers = {"Idempotency-Key": "workspace-for-browser-tab"}
    first = client.post("/api/v1/workspaces", headers=headers)
    second = client.post("/api/v1/workspaces", headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["authentication_mode"] == "local"


def test_tabular_vertical_slice_requires_review_before_analysis(client: TestClient) -> None:
    workspace_id = _create_workspace(client)
    upload_id = _upload_csv(client, workspace_id)

    sheets = client.get(f"/api/v1/tabular-uploads/{upload_id}/sheets")
    assert sheets.status_code == 200
    assert sheets.json()["sheets"] == []

    dataset_id = _create_dataset(client, upload_id)
    dataset = client.get(f"/api/v1/datasets/{dataset_id}")
    assert dataset.status_code == 200
    assert dataset.json()["status"] == "review_required"
    assert dataset.json()["profile"]["row_count"] == 10
    assert dataset.json()["schema_mapping"]["status"] == "draft"

    premature = client.post(f"/api/v1/datasets/{dataset_id}/analyses", json={})
    assert premature.status_code == 409
    assert premature.json()["error"]["code"] == "schema_mapping_not_confirmed"

    confirmed = client.put(
        f"/api/v1/datasets/{dataset_id}/schema-mapping",
        json=_schema_mapping(),
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "ready"
    assert confirmed.json()["schema_mapping"]["version"] == 1
    assert confirmed.json()["schema_mapping"]["status"] == "confirmed"

    analysis = client.post(f"/api/v1/datasets/{dataset_id}/analyses", json={})
    assert analysis.status_code == 201
    body = analysis.json()
    assert body["status"] == "ready"
    assert body["dataset_mapping_version"] == 1
    assert body["dataset_summary"]["rows"] == 10
    assert body["capabilities"]
    assert body["charts"]
    assert body["report"]["sections"]

    fetched = client.get(f"/api/v1/analyses/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_excel_sheet_discovery_and_selection_are_exposed(client: TestClient) -> None:
    workbook = BytesIO()
    with pd.ExcelWriter(workbook) as writer:
        pd.DataFrame({"amount": [10]}).to_excel(writer, sheet_name="January", index=False)
        pd.DataFrame({"amount": [20, 30]}).to_excel(
            writer,
            sheet_name="February",
            index=False,
        )
    workspace_id = _create_workspace(client)
    uploaded = client.post(
        f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
        files={
            "file": (
                "ledger.xlsx",
                workbook.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert uploaded.status_code == 201
    upload_id = uploaded.json()["id"]

    sheets = client.get(f"/api/v1/tabular-uploads/{upload_id}/sheets")
    selected = client.post(
        f"/api/v1/tabular-uploads/{upload_id}/dataset",
        json={"sheet_name": "February"},
    )

    assert sheets.json()["sheets"] == ["January", "February"]
    assert selected.status_code == 201
    assert selected.json()["sheet_name"] == "February"
    assert selected.json()["row_count"] == 2


def test_tenant_scope_is_derived_from_dependency_not_request_headers(
    api_app: FastAPI,
) -> None:
    client = TestClient(api_app)
    workspace_id = _create_workspace(client)
    upload_id = _upload_csv(client, workspace_id)
    dataset_id = _create_dataset(client, upload_id)

    api_app.dependency_overrides[get_identity] = lambda: IdentityContext(
        tenant_id="b" * 32,
        subject="other-user",
        authentication_mode="test",
        roles=frozenset({"workspace_admin"}),
    )
    try:
        responses = [
            client.get(f"/api/v1/workspaces/{workspace_id}"),
            client.get(f"/api/v1/tabular-uploads/{upload_id}/sheets"),
            client.post(f"/api/v1/tabular-uploads/{upload_id}/dataset", json={}),
            client.get(f"/api/v1/datasets/{dataset_id}"),
            client.put(
                f"/api/v1/datasets/{dataset_id}/schema-mapping",
                json=_schema_mapping(),
            ),
            client.post(f"/api/v1/datasets/{dataset_id}/analyses", json={}),
            client.post(
                f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
                files={"file": ("transactions.csv", _csv_payload(), "text/csv")},
                headers={"X-Tenant-ID": "ignored-client-value"},
            ),
        ]
    finally:
        api_app.dependency_overrides.clear()

    assert all(response.status_code == 404 for response in responses)
    assert all(response.json()["error"]["code"] == "resource_not_found" for response in responses)


def test_readiness_failure_uses_safe_service_unavailable_response() -> None:
    class UnreadyRepository(LocalResourceRepository):
        def ready(self) -> bool:
            return False

    application = create_app(
        settings=_settings(isolated_directory_path("api_unready")),
        repository=UnreadyRepository(),
    )
    response = TestClient(application).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_not_ready"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_upload_limit_is_enforced_before_resource_creation() -> None:
    application = create_app(
        settings=_settings(
            isolated_directory_path("api_upload_limit"),
            max_tabular_upload_bytes=16,
        ),
    )
    client = TestClient(application)
    workspace_id = _create_workspace(client)

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
        files={"file": ("large.csv", b"x" * 17, "text/csv")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "tabular_upload_too_large"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_validation_errors_use_safe_error_envelope(client: TestClient) -> None:
    workspace_id = _create_workspace(client)
    upload_id = _upload_csv(client, workspace_id)
    dataset_id = _create_dataset(client, upload_id)

    response = client.post(
        f"/api/v1/datasets/{dataset_id}/analyses",
        json={"anomaly_contamination": 2},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert error["details"][0]["field"] == "body.anomaly_contamination"
    assert "input" not in error["details"][0]


def test_framework_and_unexpected_errors_are_sanitized(api_app: FastAPI) -> None:
    missing = TestClient(api_app).get("/not-a-real-route")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "http_error"

    @api_app.get("/test-only/failure", include_in_schema=False)
    def fail_safely() -> None:
        raise RuntimeError("private-value-that-must-not-leak")

    failed = TestClient(api_app, raise_server_exceptions=False).get("/test-only/failure")
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "internal_error"
    assert failed.json()["error"]["request_id"] == failed.headers["X-Request-ID"]
    assert "private-value" not in failed.text


def test_invalid_mapping_and_unsupported_upload_are_rejected(client: TestClient) -> None:
    workspace_id = _create_workspace(client)
    unsupported = client.post(
        f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
        files={"file": ("notes.txt", b"not tabular", "text/plain")},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "unsupported_tabular_type"

    upload_id = _upload_csv(client, workspace_id)
    dataset_id = _create_dataset(client, upload_id)
    invalid_mapping = _schema_mapping()
    invalid_mapping["amount"] = "merchant"
    response = client.put(
        f"/api/v1/datasets/{dataset_id}/schema-mapping",
        json=invalid_mapping,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_schema_mapping"
