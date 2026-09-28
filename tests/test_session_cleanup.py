"""Tests for local session retention cleanup."""

from __future__ import annotations

import os

from src.storage.session_cleanup import cleanup_stale_sessions
from tests.test_utils import isolated_directory_path


def test_cleanup_stale_sessions_removes_only_expired_session_directories() -> None:
    tmp_path = isolated_directory_path("session_cleanup")
    sessions = tmp_path / "sessions"
    expired = sessions / ("a" * 32)
    current = sessions / ("b" * 32)
    unrelated = sessions / "do-not-delete"
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

    assert removed == [expired]
    assert not expired.exists()
    assert current.exists()
    assert unrelated.exists()
