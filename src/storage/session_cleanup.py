"""Safe retention cleanup for session-isolated local storage."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)
HEARTBEAT_FILENAME = ".heartbeat"
LAST_CLEANUP_MARKER = ".last_cleanup"
AUDIT_LOG_FILENAME = "cleanup_audit.log"
_RMTREE_ATTEMPTS = 3
_RMTREE_RETRY_DELAY_SECONDS = 0.2


def touch_session_heartbeat(session_dir: Path) -> None:
    """Record that a session directory is actively in use right now.

    Called on every render of an active session. A session directory's own
    mtime is set once at creation and never updates from writes to files
    inside it, so cleanup must check this heartbeat file's mtime instead.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / HEARTBEAT_FILENAME).touch()


def maybe_run_periodic_cleanup(
    app_data_dir: Path,
    max_age_hours: int,
    *,
    interval_seconds: float,
    exclude_session_ids: set[str] | None = None,
    now: float | None = None,
) -> list[Path]:
    """Run stale-session cleanup at most once per interval_seconds, globally.

    Decoupled from any single session's lifecycle: this runs opportunistically
    on every render of every session, but the shared marker file throttles the
    actual sweep so it only executes periodically rather than once per new tab.
    """
    current_time = now if now is not None else time.time()
    marker = app_data_dir / LAST_CLEANUP_MARKER
    if marker.exists() and (current_time - marker.stat().st_mtime) < interval_seconds:
        return []

    app_data_dir.mkdir(parents=True, exist_ok=True)
    marker.touch()
    os.utime(marker, (current_time, current_time))

    removed = cleanup_stale_sessions(
        app_data_dir,
        max_age_hours,
        exclude_session_ids=exclude_session_ids,
        now=current_time,
    )
    LOGGER.info(
        "storage_usage_bytes=%d sessions_removed=%d",
        _directory_size(app_data_dir),
        len(removed),
    )
    return removed


def cleanup_stale_sessions(
    app_data_dir: Path,
    max_age_hours: int,
    *,
    exclude_session_ids: set[str] | None = None,
    now: float | None = None,
) -> list[Path]:
    """Remove expired, correctly named session directories for every tenant."""
    if max_age_hours < 1:
        raise ValueError("max_age_hours must be at least 1.")
    sessions_root = app_data_dir / "sessions"
    if not sessions_root.exists():
        return []

    excluded = exclude_session_ids or set()
    cutoff = (now if now is not None else time.time()) - (max_age_hours * 3600)
    removed: list[Path] = []
    for tenant_dir in sessions_root.iterdir():
        if not tenant_dir.is_dir() or re.fullmatch(r"[a-f0-9]{32}", tenant_dir.name) is None:
            continue
        resolved_tenant_dir = tenant_dir.resolve()
        if resolved_tenant_dir.parent != sessions_root.resolve():
            continue
        removed.extend(
            _cleanup_tenant_sessions(app_data_dir, resolved_tenant_dir, excluded, cutoff)
        )
    return removed


def _cleanup_tenant_sessions(
    app_data_dir: Path,
    tenant_dir: Path,
    excluded: set[str],
    cutoff: float,
) -> list[Path]:
    removed: list[Path] = []
    for candidate in tenant_dir.iterdir():
        if (
            not candidate.is_dir()
            or candidate.name in excluded
            or re.fullmatch(r"[a-f0-9]{32}", candidate.name) is None
            or _last_active_time(candidate) >= cutoff
        ):
            continue
        resolved_candidate = candidate.resolve()
        if resolved_candidate.parent != tenant_dir:
            continue
        _rmtree_with_retries(resolved_candidate)
        _append_audit_record(app_data_dir, tenant_id=tenant_dir.name, session_id=candidate.name)
        removed.append(candidate)
    return removed


def _last_active_time(session_dir: Path) -> float:
    heartbeat = session_dir / HEARTBEAT_FILENAME
    if heartbeat.exists():
        return heartbeat.stat().st_mtime
    return session_dir.stat().st_mtime


def _rmtree_with_retries(path: Path) -> None:
    for attempt in range(_RMTREE_ATTEMPTS):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == _RMTREE_ATTEMPTS - 1:
                LOGGER.error("cleanup_deletion_failed attempts=%d", _RMTREE_ATTEMPTS)
                raise
            time.sleep(_RMTREE_RETRY_DELAY_SECONDS)


def _directory_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def _append_audit_record(app_data_dir: Path, *, tenant_id: str, session_id: str) -> None:
    record = {
        "timestamp": time.time(),
        "tenant_id": tenant_id,
        "session_id": session_id,
        "reason": "stale_session_expired",
    }
    audit_log = app_data_dir / AUDIT_LOG_FILENAME
    with audit_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
