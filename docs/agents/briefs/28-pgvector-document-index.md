# Brief: #28 pgvector document index (first slice)

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/28
Tier: strongest. Check mode: --full. Decision record: [ADR 0018](../../adr/0018-pgvector-document-index.md).

## Outcome

A pgvector `DocumentIndex` implementation selectable by configuration, proven by one shared contract that runs on Chroma locally and on
pgvector in CI, with fail-closed versioned migrations. The SQLite SQL guard is untouched.

## Facts (verified on 2026-10-06)

- `ChromaDocumentStore` offers `replace_chunks`, `add_chunks`, `query(where)`, `lexical_search`, `count`, `reset`, `close`; `DocumentRetriever` also calls optional `purge`.
- Workspace deletion removes the workspace directory (so Chroma needs no purge) and closes retrievers.
- No Docker, PostgreSQL, or `psycopg` on the maintainer's machine; the issue accepts CI-only Postgres tests with explicit skips.

## Do

1. `src/documents/pgvector_store.py`, `pg_migrations.py`, `scripts/migrate_postgres.py`; lazy `psycopg`; `requirements-postgres.txt`.
2. Settings (`DOCUMENT_INDEX_BACKEND`, `POSTGRES_DSN`, `POSTGRES_AUTO_MIGRATE`) fail-closed; service/API wiring with scope `<tenant>/<workspace>`; purge on workspace deletion.
3. `tests/test_document_index_contract.py` (parametrized), `tests/test_pgvector_store.py` (offline fake connection), evaluation-environment store hook for metric parity.
4. CI `pgvector` job with a service container; docs (ADR 0018, migration-and-rollback, `.env.example`).

## Do not touch

- `validate_read_query`/`execute_read_query`, the SQLite tabular path, the default Chroma behavior, image contents, default Compose.
- No Postgres metadata repository, Alembic, or Compose profile in this slice (deferred in ADR 0018).

## Acceptance

- [ ] Same contract on Chroma (always) and pgvector (CI), including isolation and atomic replacement with a concurrent reader.
- [ ] Startup verifies the schema and fails closed (missing, behind, newer); apply is idempotent and transactional.
- [ ] Every statement is scoped and parameterized (tested); every statement parses under the PostgreSQL grammar.
- [ ] DSN never in repr, logs, or script output.
- [ ] `python -m scripts.check_all --full` passes; pgvector cases skip with a clear reason locally.

## Deferred

Postgres metadata repository + Alembic (#12), Compose profile and `ops/` backup, vector-store encryption (#16), ANN index.
