"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4


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
