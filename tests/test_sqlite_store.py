"""Tests for SQLite dataframe persistence."""

from __future__ import annotations

import pandas as pd

from src.storage.sqlite_store import DEFAULT_TABLE_NAME, SQLiteStore
from tests.test_utils import isolated_database_path


def test_sqlite_store_saves_dataframe() -> None:
    database_path = isolated_database_path("store_saves_dataframe")
    dataframe = pd.DataFrame(
        {
            "amount": [10.5, 22.0],
            "merchant": ["Store A", "Store B"],
        }
    )

    stored_table = SQLiteStore(database_path).save_dataframe(
        dataframe,
        canonical_mapping={"amount": "amount"},
    )

    assert stored_table.database_path == database_path
    assert stored_table.table_name == DEFAULT_TABLE_NAME
    assert stored_table.row_count == 2
    assert stored_table.column_count == 2
    assert stored_table.columns == ["amount", "merchant"]
    assert stored_table.column_types == {"amount": "float64", "merchant": "str"}
    assert stored_table.sample_values["merchant"] == ["Store A", "Store B"]
    assert stored_table.canonical_mapping == {"amount": "amount"}
    assert SQLiteStore(database_path).table_exists(DEFAULT_TABLE_NAME)


def test_sqlite_store_rejects_unsafe_table_name() -> None:
    database_path = isolated_database_path("store_rejects_unsafe_table_name")
    dataframe = pd.DataFrame({"amount": [10.5]})

    try:
        SQLiteStore(database_path).save_dataframe(dataframe, table_name="bad-name")
    except ValueError as exc:
        assert "letters, numbers, and underscores" in str(exc)
    else:
        raise AssertionError("Expected unsafe table name to fail.")
