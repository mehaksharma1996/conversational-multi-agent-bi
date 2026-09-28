"""Tests for safe SQL execution."""

from __future__ import annotations

import pandas as pd
import pytest

from src.storage.query_executor import (
    QueryTimeoutError,
    UnsafeQueryError,
    execute_read_query,
)
from src.storage.sqlite_store import SQLiteStore
from tests.test_utils import isolated_database_path


def test_execute_read_query_allows_select() -> None:
    database_path = isolated_database_path("query_allows_select")
    SQLiteStore(database_path).save_dataframe(
        pd.DataFrame(
            {
                "amount": [10.5, 22.0, 50.0],
                "merchant": ["A", "B", "C"],
            }
        )
    )

    result = execute_read_query(
        database_path,
        "SELECT merchant, amount FROM uploaded_data WHERE amount > 20",
    )

    assert result["merchant"].tolist() == ["B", "C"]


def test_execute_read_query_rejects_write_statement() -> None:
    database_path = isolated_database_path("query_rejects_write_statement")
    SQLiteStore(database_path).save_dataframe(pd.DataFrame({"amount": [10.5]}))

    try:
        execute_read_query(database_path, "DELETE FROM uploaded_data")
    except UnsafeQueryError as exc:
        assert "Only SELECT" in str(exc)
    else:
        raise AssertionError("Expected write statement to fail.")


def test_execute_read_query_rejects_multi_statement_query() -> None:
    database_path = isolated_database_path("query_rejects_multi_statement")
    SQLiteStore(database_path).save_dataframe(pd.DataFrame({"amount": [10.5]}))

    try:
        execute_read_query(
            database_path,
            "SELECT * FROM uploaded_data; DROP TABLE uploaded_data",
        )
    except UnsafeQueryError as exc:
        assert "Only one SQL statement" in str(exc)
    else:
        raise AssertionError("Expected multi-statement query to fail.")


def test_execute_read_query_limits_large_results() -> None:
    database_path = isolated_database_path("query_limits_large_results")
    SQLiteStore(database_path).save_dataframe(pd.DataFrame({"amount": list(range(20))}))

    result = execute_read_query(
        database_path,
        "SELECT * FROM uploaded_data",
        max_rows=5,
    )

    assert len(result) == 5


def test_execute_read_query_enforces_table_allowlist() -> None:
    database_path = isolated_database_path("query_table_allowlist")
    SQLiteStore(database_path).save_dataframe(pd.DataFrame({"amount": [10.5]}))

    with pytest.raises(UnsafeQueryError):
        execute_read_query(
            database_path,
            "SELECT name FROM sqlite_master",
            allowed_tables={"uploaded_data"},
        )


def test_execute_read_query_rejects_unapproved_function() -> None:
    database_path = isolated_database_path("query_function_allowlist")
    SQLiteStore(database_path).save_dataframe(pd.DataFrame({"amount": [10.5]}))

    with pytest.raises(UnsafeQueryError):
        execute_read_query(
            database_path,
            "SELECT randomblob(10) FROM uploaded_data",
            allowed_tables={"uploaded_data"},
        )


def test_execute_read_query_times_out_expensive_query() -> None:
    database_path = isolated_database_path("query_timeout")
    SQLiteStore(database_path).save_dataframe(pd.DataFrame({"amount": list(range(500))}))

    with pytest.raises(QueryTimeoutError):
        execute_read_query(
            database_path,
            "SELECT COUNT(*) FROM uploaded_data a CROSS JOIN uploaded_data b "
            "CROSS JOIN uploaded_data c",
            allowed_tables={"uploaded_data"},
            timeout_seconds=0.001,
        )
