"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from src.utils.hashing import sha256_bytes


def isolated_database_path(test_name: str) -> Path:
    """Return a unique SQLite path inside the project workspace."""
    root = Path("work") / "test-dbs"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{test_name}_{uuid4().hex}.db"


def isolated_vector_path(test_name: str) -> Path:
    """Return a unique vector-store path inside the project workspace."""
    root = Path("work") / "test-vectors"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{test_name}_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_sha256_bytes_is_content_stable() -> None:
    assert sha256_bytes(b"business data") == sha256_bytes(b"business data")
    assert sha256_bytes(b"business data") != sha256_bytes(b"different data")
