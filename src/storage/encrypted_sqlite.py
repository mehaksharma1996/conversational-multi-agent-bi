"""SQLite connection helper supporting optional SQLCipher encryption at rest."""

from __future__ import annotations

import sqlite3
from pathlib import Path

ENCRYPTION_KEY_BYTES = 32


def connect(
    database: Path | str, *, encryption_key: bytes | None, uri: bool = False
) -> sqlite3.Connection:
    """Open a SQLite connection, transparently applying SQLCipher when keyed.

    Returns a plain stdlib connection when encryption_key is None. Otherwise
    opens the database through the SQLCipher-backed driver and immediately
    sets the raw encryption key, before any other statement runs.
    """
    if encryption_key is None:
        return sqlite3.connect(str(database), uri=uri)

    if len(encryption_key) != ENCRYPTION_KEY_BYTES:
        raise ValueError(f"encryption_key must be {ENCRYPTION_KEY_BYTES} bytes.")

    from sqlcipher3 import dbapi2 as sqlcipher

    connection = sqlcipher.connect(str(database), uri=uri)
    connection.execute(f"PRAGMA key = \"x'{encryption_key.hex()}'\"")
    return connection
