"""Safe retention cleanup for session-isolated local storage."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path


def cleanup_stale_sessions(
    app_data_dir: Path,
    max_age_hours: int,
    *,
    exclude_session_ids: set[str] | None = None,
    now: float | None = None,
) -> list[Path]:
    """Remove expired, correctly named session directories."""
    if max_age_hours < 1:
        raise ValueError("max_age_hours must be at least 1.")
    sessions_root = app_data_dir / "sessions"
    if not sessions_root.exists():
        return []

    resolved_root = sessions_root.resolve()
    excluded = exclude_session_ids or set()
    cutoff = (now if now is not None else time.time()) - (max_age_hours * 3600)
    removed: list[Path] = []
    for candidate in sessions_root.iterdir():
        if (
            not candidate.is_dir()
            or candidate.name in excluded
            or re.fullmatch(r"[a-f0-9]{32}", candidate.name) is None
            or candidate.stat().st_mtime >= cutoff
        ):
            continue
        resolved_candidate = candidate.resolve()
        if resolved_candidate.parent != resolved_root:
            continue
        shutil.rmtree(resolved_candidate)
        removed.append(candidate)
    return removed
