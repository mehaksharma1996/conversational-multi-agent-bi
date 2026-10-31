"""The UI-6b browser-journey APIs reach the states the journeys rely on, over HTTP only.

``tests/e2e/investigation_app.py`` is test-only. These checks keep its scripted wiring honest:
consent comes from the ordinary consent endpoint, and the production-shaped app fails closed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.e2e import investigation_app
from tests.test_utils import isolated_directory_path, thread_stacks

QUESTION = "Why did customer churn increase last quarter?"
CSV = (
    b"transaction_date,amount,merchant,customer_id,quarter,segment,churned\n"
    b"2026-01-01,10,Shop A,C-1,Q1,smb,1\n"
    b"2026-01-02,75,Shop B,C-2,Q1,ent,0\n"
    b"2026-04-01,20,Shop A,C-3,Q2,smb,1\n"
    b"2026-04-02,30,Shop C,C-4,Q2,ent,1\n"
)
MAPPING = {
    "amount": "amount",
    "date": "transaction_date",
    "customer_id": "customer_id",
    "merchant": "merchant",
    "location": None,
    "label": None,
}


def _app(
    monkeypatch: pytest.MonkeyPatch, name: str, build: Callable[[], FastAPI]
) -> tuple[FastAPI, Any]:
    root = isolated_directory_path(name)
    monkeypatch.setenv("E2E_DATA_DIR", str(root))
    return build(), root


def _proposal(client: TestClient) -> tuple[str, str]:
    workspace_id = client.post("/api/v1/workspaces").json()["id"]
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
        files={"file": ("churn.csv", CSV, "text/csv")},
    ).json()["id"]
    dataset_id = client.post(f"/api/v1/tabular-uploads/{upload}/dataset", json={}).json()["id"]
    assert client.put(f"/api/v1/datasets/{dataset_id}/schema-mapping", json=MAPPING).is_success
    consent = client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    )
    assert consent.is_success, consent.text
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"dataset_id": dataset_id}
    ).json()["id"]
    message = client.post(
        f"/api/v1/conversations/{conversation_id}/messages", json={"question": QUESTION}
    )
    assert message.status_code == 201 and message.json()["route"] == "investigation"
    return conversation_id, message.json()["id"]


def _until(client: TestClient, investigation_id: str, *statuses: str) -> Any:
    deadline = time.monotonic() + 60
    while True:
        body = client.get(f"/api/v1/investigations/{investigation_id}").json()
        if body["status"] in statuses:
            return body
        if time.monotonic() > deadline:
            job = client.get(f"/api/v1/jobs/{body['job_id']}").json() if body["job_id"] else None
            raise AssertionError(
                f"investigation {body['status']!r} timed out; job={job}\n{thread_stacks()}"
            )
        time.sleep(0.05)


def _start(client: TestClient) -> str:
    conversation_id, message_id = _proposal(client)
    created = client.post(
        f"/api/v1/conversations/{conversation_id}/investigations",
        json={"message_id": message_id},
        headers={"Idempotency-Key": "e2e-1"},
    )
    assert created.status_code == 202, created.text
    return str(created.json()["id"])


def test_the_scripted_app_reaches_a_sql_wave_with_task_owner_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = _app(
        monkeypatch, "e2e_investigation_scripted", investigation_app.build_scripted_app
    )
    with TestClient(app) as client:
        investigation_id = _start(client)
        plan = _until(client, investigation_id, "awaiting_plan_approval", "failed")
        assert plan["status"] == "awaiting_plan_approval", plan
        assert plan["proposal"]["cost_estimate"]["bound"] == "enforced_upper_bound"
        approved = client.post(
            f"/api/v1/investigations/{investigation_id}/plan-decision",
            json={
                "decision": "approve",
                "plan_digest": plan["plan_digest"],
                "authority_epoch": plan["authority_epoch"],
            },
        )
        assert approved.status_code == 200, approved.text
        paused = _until(client, investigation_id, "awaiting_sql_approval", "failed")
        assert paused["status"] == "awaiting_sql_approval", paused
        assert paused["progress"]["task_owners"][0]["assignee"] == "sql_analyst"
    assert not (root / investigation_app.MODEL_CALL_LOG).exists()


def test_the_production_shaped_app_fails_closed_without_a_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = _app(
        monkeypatch, "e2e_investigation_production", investigation_app.build_production_app
    )
    with TestClient(app) as client:
        investigation_id = _start(client)
        body = _until(client, investigation_id, "failed", "awaiting_plan_approval")
        assert body["status"] == "failed"
        assert body["explanation_code"] == "metering_unbounded"
        assert body["proposal"] is None and body["plan"] is None
    assert not (root / investigation_app.MODEL_CALL_LOG).exists()
