"""HTTP semantics for tenant-owned, process-local jobs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.dependencies import get_identity
from apps.api.main import create_app
from config.settings import Settings
from packages.connectors import IdentityContext
from packages.jobs import (
    InMemoryJobStore,
    InProcessJobExecutor,
    JobContext,
    JobRecord,
    JobStatus,
)
from src.utils.identity import LOCAL_DEV_TENANT_ID
from tests.test_utils import isolated_directory_path

JobRig = tuple[FastAPI, TestClient, InProcessJobExecutor]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_data_dir=tmp_path,
        sqlite_db_path=tmp_path / "sqlite" / "app.db",
        chroma_persist_dir=tmp_path / "vectorstore",
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash",
        embedding_model="all-MiniLM-L6-v2",
    )


@pytest.fixture
def job_rig() -> Iterator[JobRig]:
    executor = InProcessJobExecutor(InMemoryJobStore(), max_workers=1, max_queued=4)
    app = create_app(
        settings=_settings(isolated_directory_path("api_jobs")),
        job_executor=executor,
    )
    with TestClient(app) as client:
        yield app, client, executor


def _submit(
    executor: InProcessJobExecutor,
    operation: str = "test.run",
    work: Callable[[JobContext], object] | None = None,
) -> JobRecord:
    record, created = executor.submit(
        tenant_id=LOCAL_DEV_TENANT_ID,
        workspace_id="workspace-1",
        operation=operation,
        work=work or (lambda _context: {"private": "result is never serialized"}),
    )
    assert created
    return record


def test_job_list_uses_stable_cursor_pages_and_never_serializes_results(job_rig: JobRig) -> None:
    _, client, executor = job_rig
    for index in range(3):
        record = _submit(executor, f"test.run_{index}")
        assert (
            executor.wait(record.id, LOCAL_DEV_TENANT_ID, timeout=2).status is JobStatus.SUCCEEDED
        )

    complete = client.get("/api/v1/jobs?limit=100")
    first = client.get("/api/v1/jobs?limit=2")

    assert complete.status_code == first.status_code == 200
    expected_ids = [item["id"] for item in complete.json()["items"]]
    assert [item["id"] for item in first.json()["items"]] == expected_ids[:2]
    assert first.json()["next_cursor"]
    assert first.headers["Cache-Control"] == "no-store"
    assert "result" not in first.text and "private" not in first.text

    second = client.get(
        "/api/v1/jobs",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["items"]] == expected_ids[2:]
    assert second.json()["next_cursor"] is None

    detail = client.get(f"/api/v1/jobs/{expected_ids[0]}")
    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"

    assert client.get("/api/v1/jobs?limit=0").status_code == 422
    assert client.get("/api/v1/jobs?limit=101").status_code == 422
    invalid = client.get("/api/v1/jobs", params={"cursor": "not-a-cursor"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_cursor"


def test_job_reads_and_cancellation_hide_foreign_tenant_jobs(job_rig: JobRig) -> None:
    app, client, executor = job_rig
    record = _submit(executor)
    executor.wait(record.id, LOCAL_DEV_TENANT_ID, timeout=2)
    app.dependency_overrides[get_identity] = lambda: IdentityContext(
        tenant_id="tenant-b",
        subject="other-user",
        authentication_mode="oidc",
        roles=frozenset({"analyst"}),
    )
    try:
        assert client.get("/api/v1/jobs").json() == {"items": [], "next_cursor": None}
        missing = client.get(f"/api/v1/jobs/{record.id}")
        cancel = client.delete(f"/api/v1/jobs/{record.id}")
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == cancel.status_code == 404
    assert missing.json()["error"]["code"] == "resource_not_found"
    assert cancel.json()["error"]["code"] == "resource_not_found"


def test_cancellation_is_idempotent_and_explicitly_best_effort(job_rig: JobRig) -> None:
    _, client, executor = job_rig
    started = Event()
    release = Event()

    def stubborn(_context: JobContext) -> str:
        started.set()
        assert release.wait(timeout=2)
        return "finished"

    running = _submit(executor, "test.running", stubborn)
    assert started.wait(timeout=2)
    queued = _submit(executor, "test.queued")

    queued_cancel = client.delete(f"/api/v1/jobs/{queued.id}")
    queued_replay = client.delete(f"/api/v1/jobs/{queued.id}")
    running_cancel = client.delete(f"/api/v1/jobs/{running.id}")

    assert queued_cancel.status_code == queued_replay.status_code == 200
    assert queued_cancel.json()["status"] == "cancelled"
    assert running_cancel.status_code == 202
    assert running_cancel.json()["status"] == "running"
    assert running_cancel.json()["cancel_requested"] is True
    assert running_cancel.headers["Retry-After"] == "1"
    assert running_cancel.headers["Location"] == f"/api/v1/jobs/{running.id}"

    release.set()
    assert executor.wait(running.id, LOCAL_DEV_TENANT_ID, timeout=2).status is JobStatus.SUCCEEDED
    terminal_cancel = client.delete(f"/api/v1/jobs/{running.id}")
    assert terminal_cancel.status_code == 200
    assert terminal_cancel.json()["status"] == "succeeded"


def test_closing_a_poll_response_does_not_cancel_running_work(job_rig: JobRig) -> None:
    _, client, executor = job_rig
    started = Event()
    release = Event()

    def work(_context: JobContext) -> None:
        started.set()
        assert release.wait(timeout=2)

    record = _submit(executor, "test.disconnect", work)
    assert started.wait(timeout=2)
    with client.stream("GET", f"/api/v1/jobs/{record.id}") as response:
        assert response.status_code == 200

    assert executor.get(record.id, LOCAL_DEV_TENANT_ID).status is JobStatus.RUNNING
    release.set()
    assert executor.wait(record.id, LOCAL_DEV_TENANT_ID, timeout=2).status is JobStatus.SUCCEEDED
