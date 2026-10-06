"""Reproducibility and safety checks for the committed OpenAPI contract."""

from __future__ import annotations

import json
from pathlib import Path

from apps.api.main import create_app

OPENAPI_PATH = Path(__file__).resolve().parents[1] / "openapi" / "openapi.json"


def test_committed_openapi_contract_is_reproducible() -> None:
    committed = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))

    assert committed == create_app().openapi()


def test_openapi_contract_is_versioned_and_never_accepts_tenant_authority() -> None:
    contract = create_app().openapi()
    paths = contract["paths"]

    assert contract["info"]["version"] == "0.6.0"
    assert paths
    assert all(path.startswith("/api/v1/") for path in paths if path.startswith("/api/"))
    assert "tenant_id" not in json.dumps(contract)
    assert "/api/v1/workspaces/{workspace_id}/document-collections" in paths
    assert "/api/v1/conversations/{conversation_id}/messages" in paths
    assert "/api/v1/reports/{report_id}/content" in paths
    assert "/api/v1/workspaces/{workspace_id}" in paths
    assert "/api/v1/jobs" in paths
    assert "/api/v1/jobs/{job_id}" in paths


def test_job_openapi_contract_declares_pagination_and_cancellation_responses() -> None:
    contract = create_app().openapi()
    list_operation = contract["paths"]["/api/v1/jobs"]["get"]
    cancel_operation = contract["paths"]["/api/v1/jobs/{job_id}"]["delete"]

    parameters = {parameter["name"]: parameter for parameter in list_operation["parameters"]}
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 100
    assert "cursor" in parameters
    assert set(cancel_operation["responses"]) >= {"200", "202", "404", "422"}


def test_openapi_declares_bearer_auth_for_api_resources_but_not_health() -> None:
    contract = create_app().openapi()

    assert contract["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert contract["components"]["securitySchemes"]["APIKeyCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "__Host-bi_session",
    }
    for path, operations in contract["paths"].items():
        for operation in operations.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            if path in {"/api/v1/auth/config", "/api/v1/auth/login", "/api/v1/auth/callback"}:
                assert "security" not in operation
            elif path.startswith("/api/v1/auth/"):
                assert operation["security"] == [{"APIKeyCookie": []}]
            elif path.startswith("/api/v1/"):
                assert operation["security"] == [{"HTTPBearer": []}, {"APIKeyCookie": []}]
            if path.startswith("/health/"):
                assert "security" not in operation
