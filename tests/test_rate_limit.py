"""Deterministic rate-limit state and HTTP contract tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.rate_limit import InMemoryRateLimiter
from config.settings import Settings
from packages.observability import InMemoryTelemetrySink


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _consume(
    limiter: InMemoryRateLimiter,
    *,
    tenant: str = "tenant-a",
    workspace: str = "workspace-a",
):
    return limiter.consume(
        operation="message",
        tenant_id=tenant,
        workspace_id=workspace,
        limit=2,
        window_seconds=60,
    )


def test_limit_enforcement_reset_and_scope_independence() -> None:
    clock = Clock()
    limiter = InMemoryRateLimiter(clock=clock)

    assert _consume(limiter).allowed
    assert _consume(limiter).allowed
    denied = _consume(limiter)
    assert not denied.allowed
    assert denied.retry_after_seconds == 60

    assert _consume(limiter, tenant="tenant-b").allowed
    assert _consume(limiter, workspace="workspace-b").allowed

    clock.now += 60
    assert _consume(limiter).allowed


def test_workspace_deletion_purges_only_its_counters() -> None:
    limiter = InMemoryRateLimiter()
    _consume(limiter)
    _consume(limiter)
    _consume(limiter, workspace="workspace-b")
    _consume(limiter, workspace="workspace-b")

    limiter.delete_workspace("tenant-a", "workspace-a")

    assert _consume(limiter).allowed
    assert not _consume(limiter, workspace="workspace-b").allowed


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_data_dir=tmp_path,
        sqlite_db_path=tmp_path / "sqlite" / "app.db",
        chroma_persist_dir=tmp_path / "vectorstore",
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash",
        embedding_model="fake",
        rate_limit_window_seconds=30,
        rate_limit_tabular_uploads=1,
    )


def test_http_429_contract_has_retry_after_request_id_and_content_free_telemetry(
    tmp_path: Path,
) -> None:
    telemetry = InMemoryTelemetrySink()
    app = create_app(settings=_settings(tmp_path), telemetry_sink=telemetry)
    client = TestClient(app)
    workspace = client.post("/api/v1/workspaces").json()["id"]
    request = {"files": {"file": ("transactions.csv", b"amount\n1\n", "text/csv")}}

    assert (
        client.post(
            f"/api/v1/workspaces/{workspace}/tabular-uploads",
            **request,
        ).status_code
        == 201
    )
    limited = client.post(
        f"/api/v1/workspaces/{workspace}/tabular-uploads",
        **request,
    )

    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"
    assert limited.headers["X-Request-ID"] == limited.json()["error"]["request_id"]
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"
    events = telemetry.named("rate_limit.decision")
    assert [event.attributes["rate_limited"] for event in events] == [False, True]
    assert all(event.dropped_attributes == 0 for event in events)
