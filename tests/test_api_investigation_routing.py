"""INV-9 proposal response and persistence boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.feature_routes import router as feature_router
from apps.api.main import create_app
from apps.api.models import InvestigationProposalMessage
from apps.api.repository import MessageRecord
from apps.api.serializers import message_response
from src.orchestration.graph_state import AnswerDiagnostics, InvestigationProposalDiagnostics
from src.orchestration.investigation_routing import (
    INVESTIGATION_PROPOSAL_SCHEMA_VERSION,
    INVESTIGATION_PROPOSAL_SUMMARY,
    INVESTIGATION_ROUTE_VERSION,
)
from tests.test_api_features import _prepare_tabular_context, _settings
from tests.test_investigation_routing import _ExplodingLLM
from tests.test_utils import isolated_directory_path


def _message(**updates: object) -> MessageRecord:
    values: dict[str, object] = {
        "id": "msg_" + "1" * 32,
        "conversation_id": "conv_" + "2" * 32,
        "workspace_id": "ws_" + "3" * 32,
        "tenant_id": "4" * 32,
        "question": "Why did customer churn increase last quarter?",
        "answer": INVESTIGATION_PROPOSAL_SUMMARY,
        "route": "investigation",
        "sql": None,
        "dataframe": None,
        "sources": (),
        "created_at": datetime(2026, 10, 21, tzinfo=UTC),
        "request_id": "req-1",
        "diagnostics": InvestigationProposalDiagnostics(
            final_route="investigation",
            investigation_proposal_schema_version=INVESTIGATION_PROPOSAL_SCHEMA_VERSION,
            investigation_route_version=INVESTIGATION_ROUTE_VERSION,
            investigation_reason_codes=(
                "feature_enabled",
                "authorized_table_present",
                "metric_change_causal",
            ),
        ),
        "status": "complete",
    }
    values.update(updates)
    return MessageRecord(**values)


def test_coherent_metadata_reconstructs_a_read_only_proposal() -> None:
    response = message_response(_message())

    assert response.status == "complete"
    assert response.proposal is not None
    assert response.proposal.model_dump() == {
        "type": "investigation_proposed",
        "schema_version": 1,
        "summary": INVESTIGATION_PROPOSAL_SUMMARY,
        "request_reference": "msg_" + "1" * 32,
        "reason_codes": [
            "feature_enabled",
            "authorized_table_present",
            "metric_change_causal",
        ],
        "status": "not_started",
        "approval_required": True,
    }


def test_normal_message_shape_omits_the_optional_proposal() -> None:
    response = message_response(
        _message(
            answer="Revenue is 125.",
            route="sql",
            diagnostics=AnswerDiagnostics(final_route="sql"),
        )
    )

    assert response.proposal is None
    assert "proposal" not in response.model_dump(mode="json", exclude_unset=True)


def test_every_message_response_boundary_excludes_unset_proposals() -> None:
    message_routes = {
        (route.path, method): route
        for route in feature_router.routes
        if hasattr(route, "methods")
        for method in route.methods
        if "messages" in route.path
    }

    paths = (
        ("/api/v1/conversations/{conversation_id}/messages", "POST"),
        ("/api/v1/conversations/{conversation_id}/messages", "GET"),
        ("/api/v1/messages/{message_id}/approval", "POST"),
    )
    assert all(
        getattr(message_routes[path], "response_model_exclude_unset", False) for path in paths
    )


@pytest.mark.parametrize(
    "change",
    [
        {"route": "sql"},
        {"status": "rejected"},
        {"answer": "A legacy explanation."},
        {"sql": 'SELECT * FROM "uploaded_data"'},
        {"sources": ("memo.pdf p.1",)},
        {"diagnostics": InvestigationProposalDiagnostics(final_route="investigation")},
        {
            "diagnostics": InvestigationProposalDiagnostics(
                final_route="investigation",
                investigation_proposal_schema_version=cast(Any, 999),
                investigation_route_version=INVESTIGATION_ROUTE_VERSION,
                investigation_reason_codes=(
                    "feature_enabled",
                    "authorized_table_present",
                    "metric_change_causal",
                ),
            )
        },
        {
            "diagnostics": InvestigationProposalDiagnostics(
                final_route="investigation",
                investigation_proposal_schema_version=1,
                investigation_route_version=cast(Any, "unknown"),
                investigation_reason_codes=(
                    "feature_enabled",
                    "authorized_table_present",
                    "metric_change_causal",
                ),
            )
        },
        {
            "diagnostics": InvestigationProposalDiagnostics(
                final_route="investigation",
                investigation_proposal_schema_version=1,
                investigation_route_version=INVESTIGATION_ROUTE_VERSION,
                investigation_reason_codes=("metric_change_causal",),
            )
        },
    ],
)
def test_route_or_malformed_metadata_alone_never_reconstructs_a_proposal(change) -> None:
    assert message_response(_message(**change)).proposal is None


def test_recorded_reason_order_is_preserved_without_reclassification() -> None:
    diagnostics = replace(
        _message().diagnostics,
        investigation_reason_codes=(
            "feature_enabled",
            "authorized_table_present",
            "explicit_investigation",
            "multi_part_analysis",
        ),
    )

    proposal = message_response(_message(diagnostics=diagnostics)).proposal

    assert proposal is not None
    assert proposal.reason_codes[-2:] == ["explicit_investigation", "multi_part_analysis"]


def test_history_reconstruction_never_reruns_eligibility_or_reads_current_settings() -> None:
    with (
        patch(
            "src.orchestration.investigation_routing.evaluate_investigation_eligibility",
            side_effect=AssertionError("history must not reclassify"),
        ),
        patch(
            "config.settings.get_settings",
            side_effect=AssertionError("history must not read the current flag"),
        ),
    ):
        proposal = message_response(_message()).proposal

    assert proposal is not None
    assert proposal.reason_codes[-1] == "metric_change_causal"


def test_proposal_contract_rejects_execution_or_authority_fields() -> None:
    proposal = message_response(_message()).proposal
    assert proposal is not None

    with pytest.raises(ValidationError):
        InvestigationProposalMessage.model_validate(
            {**proposal.model_dump(), "plan_id": "plan_123", "approved": True}
        )


def test_api_passes_the_server_flag_and_returns_the_typed_proposal_without_a_model() -> None:
    root = isolated_directory_path("api-investigation-route")
    settings = replace(_settings(root), investigation_routing_enabled=True)
    app = create_app(settings=settings, llm_client_factory=lambda _settings: _ExplodingLLM())
    client = TestClient(app)
    workspace_id = client.post("/api/v1/workspaces").json()["id"]
    dataset_id, _ = _prepare_tabular_context(client, workspace_id)
    assert (
        client.put(
            f"/api/v1/workspaces/{workspace_id}/consent",
            json={"accepted": True, "notice_version": "2026-09"},
        ).status_code
        == 200
    )
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        json={"dataset_id": dataset_id},
    ).json()["id"]

    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": "Why did customer churn increase last quarter?"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["route"] == "investigation"
    assert body["status"] == "complete"
    assert body["answer"] == INVESTIGATION_PROPOSAL_SUMMARY
    assert body["proposal"]["request_reference"] == body["id"]
    assert body["proposal"]["status"] == "not_started"
    assert body["proposal"]["approval_required"] is True
    assert body["sql"] is None and body["rows"] is None and body["sources"] == []
