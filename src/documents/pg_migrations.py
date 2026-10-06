"""Versioned schema for the pgvector document index, applied or verified fail-closed.

A tiny explicit runner is used instead of Alembic on purpose (ADR 0018): the only schema here is
the document index, and Alembic belongs with the metadata repository designed under issue #12.
The version table is deliberately the simplest shape so adopting Alembic later is a one-time
stamp, not a rewrite.

* ``apply_migrations`` creates or upgrades the schema under a transaction-scoped advisory lock,
  so two processes starting together cannot race.
* ``verify_schema`` is what application startup uses by default: it never changes the database
  and raises if the schema is missing, behind, or *newer than this code understands* (a
  downgrade must not run blind).
"""

from __future__ import annotations

from typing import Any

# Arbitrary, application-specific advisory lock key for schema changes.
MIGRATION_LOCK_KEY = 7_281_903_114

MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "document_chunks",
        (
            "CREATE EXTENSION IF NOT EXISTS vector",
            """
            CREATE TABLE document_chunks (
                scope text NOT NULL,
                chunk_id text NOT NULL,
                position integer NOT NULL,
                content text NOT NULL,
                metadata jsonb NOT NULL,
                embedding vector NOT NULL,
                PRIMARY KEY (scope, chunk_id)
            )
            """,
            "CREATE INDEX document_chunks_scope_position_idx ON document_chunks (scope, position)",
        ),
    ),
)
LATEST_VERSION = MIGRATIONS[-1][0]
_VERSION_TABLE = "schema_migrations"


class SchemaError(RuntimeError):
    """The database schema does not match what this application version requires."""


def current_version(connection: Any) -> int:
    """Highest applied version, or 0 when the schema has never been created."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", (_VERSION_TABLE,))
        if cursor.fetchone()[0] is None:
            return 0
        cursor.execute(f"SELECT COALESCE(MAX(version), 0) FROM {_VERSION_TABLE}")
        return int(cursor.fetchone()[0])


def verify_schema(connection: Any) -> int:
    """Raise unless the database is exactly at the version this code requires."""
    version = current_version(connection)
    if version > LATEST_VERSION:
        raise SchemaError(
            f"The database schema is version {version}, newer than this application "
            f"understands ({LATEST_VERSION}). Upgrade the application; do not downgrade "
            "the schema blind."
        )
    if version < LATEST_VERSION:
        raise SchemaError(
            f"The database schema is version {version}, but {LATEST_VERSION} is required. "
            "Apply it with `python -m scripts.migrate_postgres`."
        )
    return version


def apply_migrations(connection: Any) -> list[int]:
    """Apply pending migrations in order; returns the versions applied. Idempotent."""
    applied: list[int] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {_VERSION_TABLE} ("
                "version integer PRIMARY KEY, name text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
        version = current_version(connection)
        if version > LATEST_VERSION:
            raise SchemaError(
                f"The database schema is version {version}, newer than this application "
                f"understands ({LATEST_VERSION}); refusing to modify it."
            )
        with connection.cursor() as cursor:
            for number, name, statements in MIGRATIONS:
                if number <= version:
                    continue
                for statement in statements:
                    cursor.execute(statement)
                cursor.execute(
                    f"INSERT INTO {_VERSION_TABLE} (version, name) VALUES (%s, %s)",
                    (number, name),
                )
                applied.append(number)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return applied
