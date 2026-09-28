"""Safe read-only SQL execution for SQLite."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pandas as pd


class UnsafeQueryError(ValueError):
    """Raised when a SQL query violates read-only safety rules."""


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


def execute_read_query(
    database_path: Path,
    query: str,
    max_rows: int = 500,
) -> pd.DataFrame:
    """Validate and execute a read-only SQLite query."""
    safe_query = validate_read_query(query)

    uri = f"file:{database_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        dataframe = pd.read_sql_query(safe_query, connection)

    if len(dataframe) > max_rows:
        return dataframe.head(max_rows)

    return dataframe


def validate_read_query(query: str) -> str:
    """Return a normalized query if it is a single read-only SELECT."""
    normalized = query.strip()
    if not normalized:
        raise UnsafeQueryError("Query cannot be empty.")

    without_trailing_semicolon = normalized[:-1].strip() if normalized.endswith(";") else normalized
    if ";" in without_trailing_semicolon:
        raise UnsafeQueryError("Only one SQL statement is allowed.")

    lowered = _strip_sql_comments(without_trailing_semicolon).lower().strip()
    if not lowered.startswith("select "):
        raise UnsafeQueryError("Only SELECT queries are allowed.")

    tokens = set(re.findall(r"[a-zA-Z_]+", lowered))
    blocked = sorted(tokens.intersection(BLOCKED_KEYWORDS))
    if blocked:
        raise UnsafeQueryError(
            "Query contains blocked keyword(s): " + ", ".join(blocked) + "."
        )

    return without_trailing_semicolon


def _strip_sql_comments(query: str) -> str:
    query = re.sub(r"--.*?$", "", query, flags=re.MULTILINE)
    query = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL)
    return query
