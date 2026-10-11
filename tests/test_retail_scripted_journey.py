"""The retail demo through the real API with a scripted provider (not a live-model test).

This proves the application path for the nine sample questions: guarded SQL, insight rendering,
document citation and policy checks, approval, and recovery, all against the committed ground truth.
The scripted provider supplies what a correct model would; see ``tests/e2e/retail_app.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.e2e import retail_app
from tests.test_utils import isolated_directory_path

SAMPLE = Path(__file__).resolve().parents[1] / "sample_data"
TRUTH = json.loads((SAMPLE / "retail_operations_ground_truth.json").read_text(encoding="utf-8"))
ANSWERS = {item["question"]: item["answer"] for item in TRUTH["questions"]}


@pytest.fixture
def client() -> Iterator[TestClient]:
    os.environ["E2E_DATA_DIR"] = str(isolated_directory_path("retail_scripted"))
    try:
        with TestClient(retail_app.build_app()) as test_client:
            yield test_client
    finally:
        os.environ.pop("E2E_DATA_DIR", None)


def _journey(client: TestClient) -> dict[str, str]:
    workspace = client.post("/api/v1/workspaces").json()["id"]
    upload = client.post(
        f"/api/v1/workspaces/{workspace}/tabular-uploads",
        files={"file": ("retail_operations.csv", (SAMPLE / "retail_operations.csv").read_bytes())},
    )
    dataset = client.post(f"/api/v1/tabular-uploads/{upload.json()['id']}/dataset", json={}).json()
    mapping = {
        field: item["source_column"]
        for field, item in dataset["schema_mapping"]["mappings"].items()
    }
    confirmed = client.put(f"/api/v1/datasets/{dataset['id']}/schema-mapping", json=mapping)
    assert confirmed.status_code == 200, confirmed.text
    documents = client.post(
        f"/api/v1/workspaces/{workspace}/document-collections",
        files=[
            (
                "files",
                (
                    "retail_operations_policy.pdf",
                    (SAMPLE / "retail_operations_policy.pdf").read_bytes(),
                    "application/pdf",
                ),
            )
        ],
    )
    assert documents.status_code == 201, documents.text
    client.put(
        f"/api/v1/workspaces/{workspace}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )
    conversation = client.post(
        f"/api/v1/workspaces/{workspace}/conversations",
        json={"dataset_id": dataset["id"], "document_collection_id": documents.json()["id"]},
    )
    assert conversation.status_code == 201, conversation.text
    return {"workspace": workspace, "conversation": conversation.json()["id"]}


def _ask(client: TestClient, conversation: str, question: str, **extra: Any) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/conversations/{conversation}/messages", json={"question": question, **extra}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _haystack(message: dict[str, Any]) -> str:
    # The insight renderer groups thousands with commas; the rows keep the raw values.
    return message["answer"].replace(",", "") + json.dumps(message["rows"])


@pytest.mark.parametrize(
    ("question", "route"),
    [
        (retail_app.TOTAL, "sql"),
        (retail_app.REGION, "sql"),
        (retail_app.CATEGORY, "sql"),
        (retail_app.MONTH, "sql"),
        (retail_app.H1, "sql"),
        (retail_app.SEGMENT, "sql"),
    ],
)
def test_table_questions_match_the_committed_ground_truth(
    client: TestClient, question: str, route: str
) -> None:
    ids = _journey(client)
    message = _ask(client, ids["conversation"], question)

    assert message["route"] == route
    assert message["status"] == "complete"
    # change_rate is a ratio in the ground truth; the answer states it as a percentage.
    expected = [str(v) for k, v in ANSWERS[question].items() if k != "change_rate"]
    haystack = _haystack(message)
    for value in expected:
        assert value in haystack, (value, haystack)
    if question == retail_app.H1:
        assert "5.2%" in message["answer"]


def test_document_question_cites_a_verified_policy_sentence(client: TestClient) -> None:
    ids = _journey(client)
    message = _ask(client, ids["conversation"], retail_app.RETURN_RULE)

    assert message["route"] == "rag"
    assert ANSWERS[retail_app.RETURN_RULE]["source_text"] in message["answer"]
    assert message["sources"], "retrieved passages must be returned"
    assert "VERIFIED_REFERENCE" in message["provenance"]["evidence_statuses"]
    assert message["provenance"]["grounding_status"] == "checked_no_issues"


@pytest.mark.parametrize(
    ("question", "key"),
    [
        (retail_app.FULFILLMENT, "transaction_count"),
        (retail_app.REVIEW, "review_transaction_count"),
    ],
)
def test_hybrid_questions_apply_the_policy_and_verify_it_structurally(
    client: TestClient, question: str, key: str
) -> None:
    ids = _journey(client)
    message = _ask(client, ids["conversation"], question)

    assert message["route"] == "hybrid"
    assert message["rows"] == [{key: ANSWERS[question][key]}]
    provenance = message["provenance"]
    assert provenance["policy_sql_status"] == "structurally_verified"
    assert provenance["policy_predicate_count"] >= 1
    assert provenance["criteria_provenance"] == "traced"
    assert message["sources"]


def test_a_rejected_approval_never_executes_the_query(client: TestClient) -> None:
    ids = _journey(client)
    pending = _ask(client, ids["conversation"], retail_app.REVIEW, require_sql_approval=True)
    assert pending["status"] == "pending_approval"
    assert pending["rows"] is None

    resolved = client.post(
        f"/api/v1/messages/{pending['id']}/approval", json={"decision": "reject"}
    )

    assert resolved.status_code == 200
    assert resolved.json()["status"] == "rejected"
    assert resolved.json()["rows"] is None
    assert resolved.json()["provenance"]["execution"]["sql_row_count"] in (None, 0)


def test_an_approved_query_runs_and_an_unsafe_edit_is_refused(client: TestClient) -> None:
    ids = _journey(client)
    pending = _ask(client, ids["conversation"], retail_app.FULFILLMENT, require_sql_approval=True)
    refused = client.post(
        f"/api/v1/messages/{pending['id']}/approval",
        json={"decision": "approve", "sql": 'DELETE FROM "uploaded_data"'},
    )
    assert refused.status_code == 422
    after = client.get(f"/api/v1/conversations/{ids['conversation']}/messages").json()["messages"]
    assert after[0]["status"] == "rejected"
    assert after[0]["rows"] is None

    again = _ask(client, ids["conversation"], retail_app.FULFILLMENT, require_sql_approval=True)
    approved = client.post(f"/api/v1/messages/{again['id']}/approval", json={"decision": "approve"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "complete"
    assert approved.json()["rows"] == [{"transaction_count": 13}]
