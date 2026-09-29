"""Tests for SQLCipher-backed encryption at rest on the tabular SQLite store."""

from __future__ import annotations

import secrets
import sqlite3

import pandas as pd
import pytest
from sqlcipher3.dbapi2 import DatabaseError as SQLCipherDatabaseError

from src.storage.query_executor import QueryTimeoutError, UnsafeQueryError, execute_read_query
from src.storage.sqlite_store import SQLiteStore
from tests.test_utils import isolated_database_path


def _sample_dataframe() -> pd.DataFrame:
    return pd.DataFrame({"amount": [10.0, 20.0, 30.0], "merchant": ["A", "B", "C"]})


def test_round_trip_with_encryption_key() -> None:
    database_path = isolated_database_path("encrypted_round_trip")
    key = secrets.token_bytes(32)

    stored_table = SQLiteStore(database_path, encryption_key=key).save_dataframe(
        _sample_dataframe()
    )

    result = execute_read_query(
        stored_table.database_path,
        f"SELECT * FROM {stored_table.table_name} ORDER BY amount",
        allowed_tables={stored_table.table_name},
        allowed_columns={stored_table.table_name: set(stored_table.columns)},
        encryption_key=key,
    )

    assert result["merchant"].tolist() == ["A", "B", "C"]


def test_encrypted_database_is_unreadable_by_plain_sqlite3() -> None:
    database_path = isolated_database_path("encrypted_opaque")
    key = secrets.token_bytes(32)
    SQLiteStore(database_path, encryption_key=key).save_dataframe(_sample_dataframe())

    connection = sqlite3.connect(str(database_path))
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("SELECT * FROM uploaded_data").fetchall()
    connection.close()


def test_wrong_key_fails_to_read() -> None:
    database_path = isolated_database_path("encrypted_wrong_key")
    key = secrets.token_bytes(32)
    wrong_key = secrets.token_bytes(32)
    stored_table = SQLiteStore(database_path, encryption_key=key).save_dataframe(
        _sample_dataframe()
    )

    with pytest.raises(SQLCipherDatabaseError):
        execute_read_query(
            stored_table.database_path,
            f"SELECT * FROM {stored_table.table_name}",
            allowed_tables={stored_table.table_name},
            allowed_columns={stored_table.table_name: set(stored_table.columns)},
            encryption_key=wrong_key,
        )


def test_none_key_behaves_like_plain_sqlite() -> None:
    database_path = isolated_database_path("encrypted_none_key")

    stored_table = SQLiteStore(database_path, encryption_key=None).save_dataframe(
        _sample_dataframe()
    )
    result = execute_read_query(
        stored_table.database_path,
        f"SELECT * FROM {stored_table.table_name}",
        allowed_tables={stored_table.table_name},
        allowed_columns={stored_table.table_name: set(stored_table.columns)},
        encryption_key=None,
    )

    assert len(result) == 3


def test_authorizer_still_enforced_under_encryption() -> None:
    database_path = isolated_database_path("encrypted_authorizer")
    key = secrets.token_bytes(32)
    stored_table = SQLiteStore(database_path, encryption_key=key).save_dataframe(
        _sample_dataframe()
    )

    with pytest.raises(UnsafeQueryError):
        execute_read_query(
            stored_table.database_path,
            "SELECT * FROM sqlite_master",
            allowed_tables={stored_table.table_name},
            allowed_columns={stored_table.table_name: set(stored_table.columns)},
            encryption_key=key,
        )


def test_timeout_still_enforced_under_encryption() -> None:
    database_path = isolated_database_path("encrypted_timeout")
    key = secrets.token_bytes(32)
    stored_table = SQLiteStore(database_path, encryption_key=key).save_dataframe(
        pd.DataFrame({"amount": list(range(500))})
    )

    with pytest.raises(QueryTimeoutError):
        execute_read_query(
            stored_table.database_path,
            (
                f"SELECT COUNT(*) FROM {stored_table.table_name} a "
                f"CROSS JOIN {stored_table.table_name} b "
                f"CROSS JOIN {stored_table.table_name} c"
            ),
            allowed_tables={stored_table.table_name},
            allowed_columns={stored_table.table_name: set(stored_table.columns)},
            encryption_key=key,
            timeout_seconds=0.001,
        )
