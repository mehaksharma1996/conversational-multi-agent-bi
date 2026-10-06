"""Datasets, analyses, and reports are rebuilt from persisted inputs after a restart (ADR 0022)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from io import BytesIO
from typing import Any

from fastapi.testclient import TestClient
from openpyxl import Workbook

from tests.test_api_durable_metadata import (
    CSV,
    Deployment,
    _as_other_tenant,
    _create_workspace,
    _upload,
    _upload_files,
)

MAPPING = {
    "amount": "amount",
    "date": "transaction_date",
    "customer_id": "customer_id",
    "merchant": "merchant",
    "location": None,
    "label": None,
}


def _dataset(client: TestClient, upload_id: str, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/v1/tabular-uploads/{upload_id}/dataset", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def _confirm(client: TestClient, dataset_id: str) -> dict[str, Any]:
    response = client.put(f"/api/v1/datasets/{dataset_id}/schema-mapping", json=MAPPING)
    assert response.status_code == 200, response.text
    confirmed: dict[str, Any] = response.json()
    return confirmed


def _analysis(client: TestClient, dataset_id: str, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/v1/datasets/{dataset_id}/analyses", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def _report(client: TestClient, analysis_id: str) -> str:
    response = client.post(f"/api/v1/analyses/{analysis_id}/reports", json={})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _stable(analysis: dict[str, Any]) -> dict[str, Any]:
    """The analysis fields that must be identical after a rebuild (not ids or timestamps)."""
    return {key: value for key, value in analysis.items() if key not in {"created_at"}}


def test_dataset_analysis_and_report_are_rebuilt_identically_after_a_restart() -> None:
    deployment = Deployment("recovered_full")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        dataset = _dataset(client, _upload(client, workspace["id"]))
        confirmed = _confirm(client, dataset["id"])
        analysis = _analysis(client, dataset["id"], anomaly_contamination=0.2)
        report_id = _report(client, analysis["id"])
        markdown = client.get(f"/api/v1/reports/{report_id}/content").content

    with TestClient(deployment.app()) as client:
        assert deployment.started()["datasets_pending"] == 1
        restored = client.get(f"/api/v1/datasets/{dataset['id']}")
        assert restored.status_code == 200
        body = restored.json()
        assert body["status"] == "ready"
        assert body["schema_mapping"] == confirmed["schema_mapping"]
        assert body["row_count"] == confirmed["row_count"]
        assert body["profile"] == confirmed["profile"]

        again = client.get(f"/api/v1/analyses/{analysis['id']}")
        assert again.status_code == 200
        assert _stable(again.json()) == _stable(analysis)
        assert client.get(f"/api/v1/reports/{report_id}/content").content == markdown
        assert client.get(f"/api/v1/reports/{report_id}/content?format=pdf").status_code == 200


def test_the_confirmed_sqlite_table_is_recreated_for_guarded_queries() -> None:
    deployment = Deployment("recovered_sqlite")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        dataset = _dataset(client, _upload(client, workspace["id"]))
        _confirm(client, dataset["id"])
    (database,) = (deployment.root / "api").glob("*/ws_*/sqlite/app.db")
    database.unlink()

    with TestClient(deployment.app()) as client:
        assert client.get(f"/api/v1/datasets/{dataset['id']}").status_code == 200

    with closing(sqlite3.connect(database)) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master")]
    assert tables, "recovery must rebuild the workspace SQLite copy that SQL answers read"


def test_each_analysis_is_rebuilt_with_the_mapping_it_originally_used() -> None:
    deployment = Deployment("recovered_mapping_history")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        dataset = _dataset(client, _upload(client, workspace["id"]))
        _confirm(client, dataset["id"])
        first = _analysis(client, dataset["id"])
        narrowed = {**MAPPING, "merchant": None, "customer_id": None}
        assert (
            client.put(
                f"/api/v1/datasets/{dataset['id']}/schema-mapping", json=narrowed
            ).status_code
            == 200
        )
        second = _analysis(client, dataset["id"])

    with TestClient(deployment.app()) as client:
        assert (
            client.get(f"/api/v1/datasets/{dataset['id']}").json()["schema_mapping"]["version"] == 2
        )
        restored_first = client.get(f"/api/v1/analyses/{first['id']}").json()
        restored_second = client.get(f"/api/v1/analyses/{second['id']}").json()

    assert _stable(restored_first) == _stable(first)
    assert _stable(restored_second) == _stable(second)
    assert restored_first["dataset_mapping_version"] == 1
    assert restored_second["dataset_mapping_version"] == 2


def test_a_recovered_dataset_supports_new_work_and_new_conversations() -> None:
    deployment = Deployment("recovered_new_work")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        dataset = _dataset(client, _upload(client, workspace["id"]))
        _confirm(client, dataset["id"])
        _analysis(client, dataset["id"])

    with TestClient(deployment.app()) as client:
        fresh = _analysis(client, dataset["id"], anomaly_contamination=0.1)
        conversation = client.post(
            f"/api/v1/workspaces/{workspace['id']}/conversations",
            json={"dataset_id": dataset["id"]},
        )
        assert conversation.status_code == 201, conversation.text
        report = client.post(f"/api/v1/analyses/{fresh['id']}/reports", json={})
        assert report.status_code == 201

    with TestClient(deployment.app()) as client:
        assert client.get(f"/api/v1/analyses/{fresh['id']}").status_code == 200


def test_an_excel_sheet_chosen_by_name_is_rebuilt_from_that_sheet() -> None:
    workbook = Workbook()
    first = workbook.active
    first.title = "Summary"
    first.append(["note"])
    first.append(["not the data"])
    second = workbook.create_sheet("Transactions")
    for row in (
        ["transaction_date", "amount", "merchant", "customer_id"],
        ["2026-01-01", 10, "A", "C-1"],
        ["2026-01-02", 75, "B", "C-2"],
        ["2026-01-03", 40, "C", "C-3"],
    ):
        second.append(row)
    buffer = BytesIO()
    workbook.save(buffer)

    deployment = Deployment("recovered_excel")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        upload = client.post(
            f"/api/v1/workspaces/{workspace['id']}/tabular-uploads",
            files={
                "file": (
                    "book.xlsx",
                    buffer.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert upload.status_code == 201, upload.text
        dataset = _dataset(client, upload.json()["id"], sheet_name="Transactions")
        assert dataset["row_count"] == 3

    with TestClient(deployment.app()) as client:
        restored = client.get(f"/api/v1/datasets/{dataset['id']}")

    assert restored.status_code == 200
    assert restored.json()["sheet_name"] == "Transactions"
    assert restored.json()["row_count"] == 3
    assert restored.json()["profile"] == dataset["profile"]


def test_recovered_resources_never_cross_tenants() -> None:
    deployment = Deployment("recovered_tenants")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        dataset = _dataset(client, _upload(client, workspace["id"]))
        _confirm(client, dataset["id"])
        analysis = _analysis(client, dataset["id"])
        report_id = _report(client, analysis["id"])

    app = deployment.app()
    with TestClient(app) as client:
        _as_other_tenant(app)
        for path in (
            f"/api/v1/datasets/{dataset['id']}",
            f"/api/v1/analyses/{analysis['id']}",
            f"/api/v1/reports/{report_id}/content",
        ):
            assert client.get(path).status_code == 404, path
        app.dependency_overrides.clear()
        assert client.get(f"/api/v1/datasets/{dataset['id']}").status_code == 200


def test_a_corrupt_upload_drops_the_dataset_and_everything_built_on_it() -> None:
    deployment = Deployment("recovered_corrupt")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        dataset = _dataset(client, _upload(client, workspace["id"]))
        _confirm(client, dataset["id"])
        analysis = _analysis(client, dataset["id"])
        report_id = _report(client, analysis["id"])
    (path,) = _upload_files(deployment)
    path.write_bytes(CSV[::-1])  # same kind of file, different bytes: the digest must catch it

    with TestClient(deployment.app()) as client:
        assert client.get(f"/api/v1/datasets/{dataset['id']}").status_code == 404
        assert client.get(f"/api/v1/analyses/{analysis['id']}").status_code == 404
        assert client.get(f"/api/v1/reports/{report_id}/content").status_code == 404
        assert client.get(f"/api/v1/workspaces/{workspace['id']}").status_code == 200

    with closing(sqlite3.connect(deployment.metadata_db)) as connection:
        for table in ("datasets", "analyses", "reports"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,), table


def test_deleting_a_workspace_removes_its_recovered_resources_everywhere() -> None:
    deployment = Deployment("recovered_delete")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        dataset = _dataset(client, _upload(client, workspace["id"]))
        _confirm(client, dataset["id"])
        _analysis(client, dataset["id"])

    with TestClient(deployment.app()) as client:
        assert client.delete(f"/api/v1/workspaces/{workspace['id']}").status_code == 204
        assert client.get(f"/api/v1/datasets/{dataset['id']}").status_code == 404

    with TestClient(deployment.app()) as client:
        assert client.get(f"/api/v1/datasets/{dataset['id']}").status_code == 404
        assert deployment.started()["datasets_pending"] == 0
