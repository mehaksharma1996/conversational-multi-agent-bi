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

    assert contract["info"]["version"] == "0.5.0"
    assert paths
    assert all(path.startswith("/api/v1/") for path in paths if path.startswith("/api/"))
    assert "tenant_id" not in json.dumps(contract)
    assert "/api/v1/workspaces/{workspace_id}/document-collections" in paths
    assert "/api/v1/conversations/{conversation_id}/messages" in paths
    assert "/api/v1/reports/{report_id}/content" in paths
    assert "/api/v1/workspaces/{workspace_id}" in paths
