"""Apply or verify the pgvector document-index schema.

    $env:POSTGRES_DSN = "postgresql://user:password@127.0.0.1:5432/bi"   # from your environment
    python -m scripts.migrate_postgres            # apply pending migrations
    python -m scripts.migrate_postgres --verify   # read-only check; fails if not exactly current

Exit status: 0 success, 1 the schema is wrong (verify) or a migration failed, 2 usage or
configuration error. The DSN is read from the environment and is never printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from contextlib import closing
from typing import Any

from src.documents.pg_migrations import (
    LATEST_VERSION,
    SchemaError,
    apply_migrations,
    current_version,
    verify_schema,
)
from src.documents.pgvector_store import psycopg_connection_factory


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    connect: Any = None,
) -> int:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Do not change the database.")
    args = parser.parse_args(argv)

    dsn = env.get("POSTGRES_DSN", "").strip()
    if connect is None:
        if not dsn:
            print("POSTGRES_DSN is not set.", file=sys.stderr)
            return 2
        connect = psycopg_connection_factory(dsn)
    try:
        with closing(connect()) as connection:
            if args.verify:
                verify_schema(connection)
                connection.rollback()
                print(f"Schema is current (version {LATEST_VERSION}).")
                return 0
            applied = apply_migrations(connection)
            version = current_version(connection)
            connection.rollback()
    except SchemaError as exc:
        print(f"Schema check failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # never echo a DSN or driver detail beyond the class name
        print(f"Migration failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(f"Applied {applied or 'nothing'}; schema is at version {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
