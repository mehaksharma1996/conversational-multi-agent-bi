"""Safe read-only SQL execution for SQLite."""

from __future__ import annotations

import logging
import sqlite3
import warnings
from contextlib import closing
from pathlib import Path
from time import monotonic

import pandas as pd
import sqlparse
from sqlparse import tokens as sql_tokens

from src.storage import encrypted_sqlite

LOGGER = logging.getLogger(__name__)
_PANDAS_DBAPI2_WARNING = "pandas only supports SQLAlchemy connectable.*"


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
    "iif",
    "instr",
    "julianday",
    "length",
    "lower",
    "ltrim",
    "max",
    "min",
    "nullif",
    "printf",
    "replace",
    "round",
    "rtrim",
    "strftime",
    "substr",
    "sum",
    "group_concat",
    "total",
    "trim",
    "typeof",
    "upper",
    "glob",
    "like",
    "row_number",
    "rank",
    "dense_rank",
    "lag",
    "lead",
}


def execute_read_query(
    database_path: Path,
    query: str,
    max_rows: int = 500,
    allowed_tables: set[str] | None = None,
    allowed_columns: dict[str, set[str]] | None = None,
    timeout_seconds: float = 5.0,
    encryption_key: bytes | None = None,
) -> pd.DataFrame:
    """Validate and execute a read-only SQLite query."""
    if max_rows < 1:
        raise ValueError("max_rows must be at least 1.")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0.")

    safe_query = validate_read_query(query)
    limited_query = f"SELECT * FROM ({safe_query}) AS __limited_result LIMIT {int(max_rows)}"

    uri = f"file:{database_path.as_posix()}?mode=ro"
    with closing(
        encrypted_sqlite.connect(uri, encryption_key=encryption_key, uri=True)
    ) as connection:
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
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=_PANDAS_DBAPI2_WARNING)
                return pd.read_sql_query(limited_query, connection)
        except Exception as exc:
            message = str(exc).lower()
            if "interrupted" in message:
                LOGGER.warning("query_timeout timeout_seconds=%.2f", timeout_seconds)
                raise QueryTimeoutError(
                    f"Query exceeded the {timeout_seconds:g}-second execution limit."
                ) from exc
            if "not authorized" in message or "prohibited" in message:
                LOGGER.warning("query_rejected_unsafe")
                raise UnsafeQueryError(
                    "Query referenced a table, column, or function that is not allowed."
                ) from exc
            raise


def validate_read_query(query: str) -> str:
    """Return a normalized query if it is one read-only SELECT or CTE query.

    The statement is parsed so literal and comment contents cannot trigger
    separator or keyword checks. SQLite's authorizer remains the final safeguard
    at execution time.
    """
    normalized = query.strip()
    if not normalized:
        raise UnsafeQueryError("Query cannot be empty.")

    split_statements = [statement for statement in sqlparse.split(normalized) if statement.strip()]
    if len(split_statements) != 1:
        raise UnsafeQueryError("Only one SQL statement is allowed.")

    statement_text = split_statements[0].strip()
    parsed_statements = sqlparse.parse(statement_text)
    if len(parsed_statements) != 1 or parsed_statements[0].get_type() != "SELECT":
        raise UnsafeQueryError("Only SELECT queries and read-only CTEs are allowed.")

    keyword_values = {
        token.normalized.lower()
        for token in parsed_statements[0].flatten()
        if token.ttype in sql_tokens.Keyword
        and token.ttype not in sql_tokens.Literal.String
        and token.ttype not in sql_tokens.Comment
    }
    blocked = sorted(keyword_values.intersection(BLOCKED_KEYWORDS))
    if blocked:
        raise UnsafeQueryError("Query contains blocked keyword(s): " + ", ".join(blocked) + ".")

    return statement_text[:-1].rstrip() if statement_text.endswith(";") else statement_text


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
