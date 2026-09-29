"""Tests for local session retention cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.storage import session_cleanup
from src.storage.session_cleanup import (
    AUDIT_LOG_FILENAME,
    HEARTBEAT_FILENAME,
    cleanup_stale_sessions,
    maybe_run_periodic_cleanup,
)
from tests.test_utils import isolated_directory_path


def _resolved(paths: list[Path]) -> list[Path]:
    """Compare directories by canonical location.

    Cleanup resolves tenant directories before deleting, so it reports canonical
    paths. A temp directory reached through a junction, symlink, or Windows 8.3
    short name is equal by location but not by string.
    """
    return [path.resolve() for path in paths]


def test_cleanup_stale_sessions_removes_only_expired_session_directories() -> None:
    tmp_path = isolated_directory_path("session_cleanup")
    tenant = tmp_path / "sessions" / ("1" * 32)
    expired = tenant / ("a" * 32)
    current = tenant / ("b" * 32)
    unrelated = tenant / "do-not-delete"
    expired.mkdir(parents=True)
    current.mkdir()
    unrelated.mkdir()
    old_time = 1_000.0
    os.utime(expired, (old_time, old_time))
    os.utime(current, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))

    removed = cleanup_stale_sessions(
        tmp_path,
        max_age_hours=1,
        exclude_session_ids={current.name},
        now=old_time + 7_200,
    )

    assert _resolved(removed) == _resolved([expired])
    assert not expired.exists()
    assert current.exists()
    assert unrelated.exists()


def test_cleanup_stale_sessions_skips_malformed_tenant_directories() -> None:
    tmp_path = isolated_directory_path("session_cleanup_malformed_tenant")
    sessions = tmp_path / "sessions"
    malformed_tenant = sessions / "not-a-tenant"
    expired_looking = malformed_tenant / ("a" * 32)
    expired_looking.mkdir(parents=True)
    old_time = 1_000.0
    os.utime(expired_looking, (old_time, old_time))

    removed = cleanup_stale_sessions(
        tmp_path,
        max_age_hours=1,
        now=old_time + 7_200,
    )

    assert removed == []
    assert expired_looking.exists()


def test_cleanup_stale_sessions_never_crosses_tenant_boundaries() -> None:
    """A session_id reused under two tenants must be evaluated independently."""
    tmp_path = isolated_directory_path("session_cleanup_cross_tenant")
    sessions = tmp_path / "sessions"
    tenant_a_session = sessions / ("a" * 32) / ("c" * 32)
    tenant_b_session = sessions / ("b" * 32) / ("c" * 32)
    tenant_a_session.mkdir(parents=True)
    tenant_b_session.mkdir(parents=True)
    old_time = 1_000.0
    fresh_time = 1_000_000.0
    os.utime(tenant_a_session, (old_time, old_time))
    os.utime(tenant_b_session, (fresh_time, fresh_time))

    removed = cleanup_stale_sessions(
        tmp_path,
        max_age_hours=1,
        now=old_time + 7_200,
    )

    assert _resolved(removed) == _resolved([tenant_a_session])
    assert not tenant_a_session.exists()
    assert tenant_b_session.exists()


def test_cleanup_stale_sessions_prefers_heartbeat_over_directory_mtime() -> None:
    """The root-cause regression test: a directory's own mtime never updates
    from writes to files inside it, so an active session must be judged by
    its heartbeat file, not by the directory's stale creation-time mtime.
    """
    tmp_path = isolated_directory_path("session_cleanup_heartbeat_fresh")
    tenant = tmp_path / "sessions" / ("2" * 32)
    active = tenant / ("d" * 32)
    active.mkdir(parents=True)
    old_time = 1_000.0
    fresh_time = 1_000_000.0
    os.utime(active, (old_time, old_time))
    heartbeat = active / HEARTBEAT_FILENAME
    heartbeat.touch()
    os.utime(heartbeat, (fresh_time, fresh_time))

    removed = cleanup_stale_sessions(tmp_path, max_age_hours=1, now=old_time + 7_200)

    assert removed == []
    assert active.exists()


def test_cleanup_stale_sessions_removes_when_heartbeat_is_also_stale() -> None:
    tmp_path = isolated_directory_path("session_cleanup_heartbeat_stale")
    tenant = tmp_path / "sessions" / ("3" * 32)
    idle = tenant / ("e" * 32)
    idle.mkdir(parents=True)
    old_time = 1_000.0
    heartbeat = idle / HEARTBEAT_FILENAME
    heartbeat.touch()
    os.utime(idle, (old_time, old_time))
    os.utime(heartbeat, (old_time, old_time))

    removed = cleanup_stale_sessions(tmp_path, max_age_hours=1, now=old_time + 7_200)

    assert _resolved(removed) == _resolved([idle])
    assert not idle.exists()


def test_cleanup_stale_sessions_retries_transient_rmtree_failures(monkeypatch) -> None:
    tmp_path = isolated_directory_path("session_cleanup_retry")
    tenant = tmp_path / "sessions" / ("4" * 32)
    expired = tenant / ("f" * 32)
    expired.mkdir(parents=True)
    old_time = 1_000.0
    os.utime(expired, (old_time, old_time))

    real_rmtree = session_cleanup.shutil.rmtree
    call_count = {"n": 0}

    def flaky_rmtree(path, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise PermissionError("simulated transient lock")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(session_cleanup.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(session_cleanup.time, "sleep", lambda _seconds: None)

    removed = cleanup_stale_sessions(tmp_path, max_age_hours=1, now=old_time + 7_200)

    assert _resolved(removed) == _resolved([expired])
    assert not expired.exists()
    assert call_count["n"] == 2


def test_cleanup_deletion_failure_is_logged_before_reraising(monkeypatch, caplog) -> None:
    tmp_path = isolated_directory_path("session_cleanup_permanent_failure")
    tenant = tmp_path / "sessions" / ("7" * 32)
    expired = tenant / ("1" * 32)
    expired.mkdir(parents=True)
    old_time = 1_000.0
    os.utime(expired, (old_time, old_time))

    def always_fails(path, *args, **kwargs):
        raise PermissionError("simulated permanent lock")

    monkeypatch.setattr(session_cleanup.shutil, "rmtree", always_fails)
    monkeypatch.setattr(session_cleanup.time, "sleep", lambda _seconds: None)

    with caplog.at_level("ERROR"):
        try:
            cleanup_stale_sessions(tmp_path, max_age_hours=1, now=old_time + 7_200)
            raised = False
        except PermissionError:
            raised = True

    assert raised
    assert any("cleanup_deletion_failed" in record.getMessage() for record in caplog.records)


def test_cleanup_stale_sessions_writes_audit_record_for_each_removal() -> None:
    tmp_path = isolated_directory_path("session_cleanup_audit")
    tenant_id = "5" * 32
    session_id = "0" * 32
    expired = tmp_path / "sessions" / tenant_id / session_id
    expired.mkdir(parents=True)
    old_time = 1_000.0
    os.utime(expired, (old_time, old_time))

    cleanup_stale_sessions(tmp_path, max_age_hours=1, now=old_time + 7_200)

    audit_log = tmp_path / AUDIT_LOG_FILENAME
    assert audit_log.exists()
    records = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["tenant_id"] == tenant_id
    assert records[0]["session_id"] == session_id
    assert records[0]["reason"] == "stale_session_expired"


def test_maybe_run_periodic_cleanup_throttles_repeat_calls() -> None:
    tmp_path = isolated_directory_path("session_cleanup_throttle")
    tenant = tmp_path / "sessions" / ("6" * 32)
    expired = tenant / ("9" * 32)
    expired.mkdir(parents=True)
    old_time = 1_000.0
    os.utime(expired, (old_time, old_time))
    now = old_time + 7_200

    first = maybe_run_periodic_cleanup(tmp_path, max_age_hours=1, interval_seconds=600, now=now)
    assert _resolved(first) == _resolved([expired])
    assert not expired.exists()

    expired.mkdir(parents=True)
    os.utime(expired, (old_time, old_time))
    second = maybe_run_periodic_cleanup(
        tmp_path, max_age_hours=1, interval_seconds=600, now=now + 10
    )
    assert second == []
    assert expired.exists()

    third = maybe_run_periodic_cleanup(
        tmp_path, max_age_hours=1, interval_seconds=600, now=now + 700
    )
    assert _resolved(third) == _resolved([expired])
    assert not expired.exists()


def test_maybe_run_periodic_cleanup_logs_storage_usage(caplog) -> None:
    tmp_path = isolated_directory_path("session_cleanup_storage_metric")
    session_dir = tmp_path / "sessions" / ("8" * 32) / ("2" * 32)
    session_dir.mkdir(parents=True)
    (session_dir / "data.txt").write_bytes(b"x" * 1024)

    with caplog.at_level("INFO"):
        maybe_run_periodic_cleanup(tmp_path, max_age_hours=1, interval_seconds=600, now=1_000.0)

    matching = [
        record.getMessage()
        for record in caplog.records
        if "storage_usage_bytes" in record.getMessage()
    ]
    assert matching
    assert "storage_usage_bytes=" in matching[0]
