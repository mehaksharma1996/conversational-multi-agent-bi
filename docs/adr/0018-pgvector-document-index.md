# ADR 0018: pgvector document index behind the existing port; user-data SQL stays on SQLite

- Status: Accepted
- Date: 2026-10-06
- Issue: [#28](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/28) (first slice)
- Coordinates with [#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12) (durable metadata) and #16 (vector-store encryption).

## Context

`packages/connectors/ports.py` defines `DocumentIndex` and `TabularRepository`, but each had one implementation (Chroma, SQLite).
A second implementation proves the boundary is real. Docker and PostgreSQL are not available on the maintainer's machine, so
what can be verified locally and what must be verified in CI are deliberately separated.

## Decisions

1. **User-data SQL stays on SQLite.** `validate_read_query`/`execute_read_query` depend on SQLite features (a `mode=ro` URI,
   `set_authorizer` table/column/function allowlisting, a progress-handler deadline). None of that translates to Postgres, and the
   guard is not weakened or duplicated. A Postgres tabular backend needs its own design (read-only role, restricted views,
   `statement_timeout`, function allowlist, row cap) and evaluation evidence first. The document index stores *document chunks* and runs only
   constant, parameterized statements, never user-authored SQL.
2. **`PgvectorDocumentStore`** implements the same contract as `ChromaDocumentStore` (`replace_chunks`, `add_chunks`, `query`, `lexical_search`,
   `count`, `reset`, plus `purge`). It is selected by `DOCUMENT_INDEX_BACKEND=pgvector`; SQLite + Chroma remains the zero-dependency default and
   `psycopg` is an optional requirement (`requirements-postgres.txt`) imported lazily, so default installs and images do not change.
3. **Isolation by construction.** Every row carries a `scope` (`<tenant>/<workspace>`, set by server code) and every statement filters on it with a
   bound parameter. There is no statement without a scope predicate (a test asserts this). Deleting a workspace purges its rows, because a database-backed index
   is not removed with the workspace directory.
4. **Atomic replacement via MVCC.** `replace_chunks` embeds everything first, then deletes the scope's rows and inserts the new ones in one transaction:
   concurrent readers see the previous index until commit, and any failure rolls back. No staging collection exists to clean up. The shared contract proves
   this with a concurrent-reader test on both backends.
5. **Fail-closed schema.** Versioned migrations (`src/documents/pg_migrations.py`) are applied by `python -m scripts.migrate_postgres` (or `POSTGRES_AUTO_MIGRATE=true`)
   under a transaction-scoped advisory lock. Startup only *verifies*: a missing or behind schema, or a schema *newer* than the code, refuses to run; apply never touches a newer schema.
6. **Credentials from the environment only.** `POSTGRES_DSN` is read from the environment, excluded from `repr`, held only inside a connection factory, never logged,
   and never echoed by the migration script. The backend fails closed if selected without a `postgresql://` DSN.
7. **Verification split.** The Chroma cases of the shared contract always run. The pgvector cases are skipped locally with an explicit reason unless `PGVECTOR_TEST_DSN` is
   set, and run in the CI `pgvector` job against a service container (including retrieval-metric parity with the committed thresholds). Offline tests pin the
   properties that need no database (bound parameters, scope on every statement, transaction boundaries, migration state machine), and every statement was
   validated against the PostgreSQL grammar with `pglast`. Syntax is not semantics: the CI run is the proof.

## Deferred (explicit)

- **A Postgres metadata repository and Alembic migrations.** The metadata schema belongs to #12's design; building it here would split that work. A tiny explicit runner is
  used for the index schema now, with the simplest version-table shape so adopting Alembic later is a one-time stamp.
- **Compose profile and `ops/` backup/restore for Postgres.** They require a digest-pinned, non-root Postgres+pgvector image that cannot be validated without Docker; they
  should land with the CI `containers` job able to prove them.
- **Vector-store encryption** (#16) and an ANN index (HNSW needs a fixed dimension; exact search is adequate for the chunk limits).

## Consequences

- One embedding dimension per database: the `vector` column is unconstrained so scopes can differ across deployments, but mixing dimensions in one database makes distance
  evaluation error. The embedding model is a global setting, so this holds in practice; changing models requires re-indexing.
- `CREATE EXTENSION vector` needs sufficient privileges at migration time; run the migration with a privileged role and the application with a restricted one.
- The BM25 index is rebuilt from the scope's rows when a content fingerprint changes, costing one aggregate query per lexical search.

## Invariants

- User-data SQL guard is unchanged and SQLite-only.
- No statement runs without a scope predicate; user content is only ever a bound parameter.
- Startup never modifies a database it has not been told to migrate.
