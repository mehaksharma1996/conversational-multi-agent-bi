"""SQLite persistence for uploaded tabular data."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

DEFAULT_TABLE_NAME = "uploaded_data"


@dataclass(frozen=True)
class StoredTable:
    database_path: Path
    table_name: str
    row_count: int
    column_count: int
    columns: list[str]
    column_types: dict[str, str] = field(default_factory=dict)
    sample_values: dict[str, list[str]] = field(default_factory=dict)
    canonical_mapping: dict[str, str] = field(default_factory=dict)


class SQLiteStore:
    """Small SQLite wrapper for dataframe persistence."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def save_dataframe(
        self,
        dataframe: pd.DataFrame,
        table_name: str = DEFAULT_TABLE_NAME,
        canonical_mapping: dict[str, str] | None = None,
    ) -> StoredTable:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        safe_table_name = validate_identifier(table_name)

        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                dataframe.to_sql(
                    safe_table_name,
                    connection,
                    if_exists="replace",
                    index=False,
                )

        return StoredTable(
            database_path=self.database_path,
            table_name=safe_table_name,
            row_count=len(dataframe),
            column_count=len(dataframe.columns),
            columns=[str(column) for column in dataframe.columns],
            column_types={
                str(column): str(dataframe[column].dtype) for column in dataframe.columns
            },
            sample_values={
                str(column): [str(value) for value in dataframe[column].dropna().unique()[:5]]
                for column in dataframe.columns
            },
            canonical_mapping=dict(canonical_mapping or {}),
        )

    def table_exists(self, table_name: str = DEFAULT_TABLE_NAME) -> bool:
        safe_table_name = validate_identifier(table_name)
        query = """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
        """
        with closing(sqlite3.connect(self.database_path)) as connection:
            result = connection.execute(query, (safe_table_name,)).fetchone()
        return result is not None


def validate_identifier(identifier: str) -> str:
    """Validate a SQLite identifier used by code-owned SQL."""
    if not identifier:
        raise ValueError("Identifier cannot be empty.")

    if not identifier.replace("_", "").isalnum():
        raise ValueError("Identifier may only contain letters, numbers, and underscores.")

    if identifier[0].isdigit():
        raise ValueError("Identifier cannot start with a number.")

    return identifier
