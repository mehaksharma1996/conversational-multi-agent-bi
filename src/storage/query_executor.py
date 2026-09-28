"""Safe read-only SQL execution for SQLite."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

import pandas as pd


class UnsafeQueryError(ValueError):
    """Raised when a SQL query violates read-only safety rules."""


class QueryTimeoutError(TimeoutError):
    """Raised when a read query exceeds its execution deadline."""


BLOCKED_KEYWORDS = {
    "alter",
    "attach",
    "create",
    "delete",
    "detach",
    "drop",
    "insert",
    "pragma",
    "replace",
    "update",
    "vacuum",
}

ALLOWED_FUNCTIONS = {
    "abs",
    "avg",
    "coalesce",
    "count",
    "date",
    "datetime",
    "ifnull",
    "julianday",
    "length",
    "lower",
    "ltrim",
    "max",
    "min",
    "nullif",
    "printf",
    "round",
    "rtrim",
    "strftime",
    "substr",
    "sum",
    "total",
    "trim",
    "typeof",
    "upper",
}


def execute_read_query(
    database_path: Path,
    query: str,
    max_rows: int = 500,
    allowed_tables: set[str] | None = None,
    allowed_columns: dict[str, set[str]] | None = None,
    timeout_seconds: float = 5.0,
) -> pd.DataFrame:
    """Validate and execute a read-only SQLite query."""
    if max_rows < 1:
        raise ValueError("max_rows must be at least 1.")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0.")

    safe_query = validate_read_query(query)
    limited_query = f"SELECT * FROM ({safe_query}) AS __limited_result LIMIT {int(max_rows)}"

    uri = f"file:{database_path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        deadline = monotonic() + timeout_seconds
        connection.set_progress_handler(
            lambda: 1 if monotonic() > deadline else 0,
            1_000,
        )
        connection.set_authorizer(
            _build_authorizer(
                allowed_tables=allowed_tables,
                allowed_columns=allowed_columns,
            )
        )
        try:
            return pd.read_sql_query(limited_query, connection)
        except Exception as exc:
            message = str(exc).lower()
            if "interrupted" in message:
                raise QueryTimeoutError(
                    f"Query exceeded the {timeout_seconds:g}-second execution limit."
                ) from exc
            if "not authorized" in message or "prohibited" in message:
                raise UnsafeQueryError(
                    "Query referenced a table, column, or function that is not allowed."
                ) from exc
            raise


def validate_read_query(query: str) -> str:
    """Return a normalized query if it is a single read-only SELECT."""
    normalized = query.strip()
    if not normalized:
        raise UnsafeQueryError("Query cannot be empty.")

    without_trailing_semicolon = normalized[:-1].strip() if normalized.endswith(";") else normalized
    if ";" in without_trailing_semicolon:
        raise UnsafeQueryError("Only one SQL statement is allowed.")

    lowered = _strip_sql_comments(without_trailing_semicolon).lower().strip()
    if re.match(r"^select\b", lowered) is None:
        raise UnsafeQueryError("Only SELECT queries are allowed.")

    tokens = set(re.findall(r"[a-zA-Z_]+", lowered))
    blocked = sorted(tokens.intersection(BLOCKED_KEYWORDS))
    if blocked:
        raise UnsafeQueryError("Query contains blocked keyword(s): " + ", ".join(blocked) + ".")

    return without_trailing_semicolon


def _build_authorizer(
    allowed_tables: set[str] | None,
    allowed_columns: dict[str, set[str]] | None,
):
    normalized_tables = (
        {table.lower() for table in allowed_tables} if allowed_tables is not None else None
    )
    normalized_columns = {
        table.lower(): {column.lower() for column in columns}
        for table, columns in (allowed_columns or {}).items()
    }

    def authorize(
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ:
            table = (argument_one or "").lower()
            column = (argument_two or "").lower()
            if normalized_tables is not None and table not in normalized_tables:
                return sqlite3.SQLITE_DENY
            table_columns = normalized_columns.get(table)
            if table_columns is not None and column and column not in table_columns:
                return sqlite3.SQLITE_DENY

        if action == sqlite3.SQLITE_FUNCTION:
            function_name = (argument_two or argument_one or "").lower()
            if function_name not in ALLOWED_FUNCTIONS:
                return sqlite3.SQLITE_DENY

        return sqlite3.SQLITE_OK

    return authorize


def _strip_sql_comments(query: str) -> str:
    query = re.sub(r"--.*?$", "", query, flags=re.MULTILINE)
    query = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL)
    return query
