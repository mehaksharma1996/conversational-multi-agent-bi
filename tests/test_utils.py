"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from src.utils.hashing import sha256_bytes

_TEST_WORKSPACE = TemporaryDirectory(prefix="conversational-bi-tests-", ignore_cleanup_errors=True)


def isolated_database_path(test_name: str) -> Path:
    """Return a disposable SQLite path outside the repository."""
    root = Path(_TEST_WORKSPACE.name) / "test-dbs"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{test_name}_{uuid4().hex}.db"


def isolated_vector_path(test_name: str) -> Path:
    """Return a disposable vector-store path outside the repository."""
    root = Path(_TEST_WORKSPACE.name) / "test-vectors"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{test_name}_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def isolated_directory_path(test_name: str) -> Path:
    """Return a unique disposable directory for a test."""
    path = Path(_TEST_WORKSPACE.name) / f"{test_name}_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_sha256_bytes_is_content_stable() -> None:
    assert sha256_bytes(b"business data") == sha256_bytes(b"business data")
    assert sha256_bytes(b"business data") != sha256_bytes(b"different data")
