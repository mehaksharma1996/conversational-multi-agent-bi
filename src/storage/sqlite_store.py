"""SQLite persistence for uploaded tabular data."""

from __future__ import annotations

import warnings
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.storage import encrypted_sqlite

DEFAULT_TABLE_NAME = "uploaded_data"
_PANDAS_DBAPI2_WARNING = "pandas only supports SQLAlchemy connectable.*"


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
    encryption_key: bytes | None = None


class SQLiteStore:
    """Small SQLite wrapper for dataframe persistence."""

    def __init__(
        self,
        database_path: Path,
        encryption_key: bytes | None = None,
        include_sample_values: bool = True,
    ) -> None:
        self.database_path = database_path
        self.encryption_key = encryption_key
        self.include_sample_values = include_sample_values

    def save_dataframe(
        self,
        dataframe: pd.DataFrame,
        table_name: str = DEFAULT_TABLE_NAME,
        canonical_mapping: dict[str, str] | None = None,
    ) -> StoredTable:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        safe_table_name = validate_identifier(table_name)

        with closing(
            encrypted_sqlite.connect(self.database_path, encryption_key=self.encryption_key)
        ) as connection:
            with connection:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message=_PANDAS_DBAPI2_WARNING)
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
            sample_values=(
                {
                    str(column): [str(value) for value in dataframe[column].dropna().unique()[:5]]
                    for column in dataframe.columns
                }
                if self.include_sample_values
                else {}
            ),
            canonical_mapping=dict(canonical_mapping or {}),
            encryption_key=self.encryption_key,
        )

    def table_exists(self, table_name: str = DEFAULT_TABLE_NAME) -> bool:
        safe_table_name = validate_identifier(table_name)
        query = """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
        """
        with closing(
            encrypted_sqlite.connect(self.database_path, encryption_key=self.encryption_key)
        ) as connection:
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
