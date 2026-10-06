# Migrating from Streamlit and rolling back

ADR 0011 makes React/FastAPI the primary local product and keeps Streamlit as a developer-only
compatibility surface. The two surfaces do not share an active workspace.

## Move a user to React/Compose

1. Warn the user that in-progress Streamlit and API workspaces do not migrate. Download any report
   or query result that must be kept.
2. Start `docker compose up -d --build --wait` and open <http://127.0.0.1:8080>.
3. Re-upload the original CSV/Excel/PDF inputs, review the inferred schema, and rerun analysis.
4. In Gemini mode, accept the data-sharing notice again. Consent and chat history are scoped to the
   new API workspace.
5. Verify the expected report/export, then reset the old Streamlit session if it is still running.

What carries over: source files the user retained, exported reports/results, configuration supplied
through environment variables, the model cache, and the content-free audit chain. What does not:
workspace IDs, uploads, profiles, confirmed mappings, analyses, chat history, consent, reports,
exports, SQLite tables, or Chroma indexes. Workspaces in the React/Compose product keep their own
durable records (ADR 0022), but nothing is shared with Streamlit.

## Run both surfaces side by side

```powershell
docker compose up -d --build --wait
docker compose --profile streamlit up -d --wait streamlit
```

Use React at `127.0.0.1:8080` and Streamlit at `127.0.0.1:8501`. They use distinct workspace roots.
Do not treat a result in one surface as proof that the other is operating on the same dataset.
Streamlit remains for diagnosis and compatibility comparison, not for new product features.

## Workspace metadata: schema upgrades and the rollback boundary

Durable workspace metadata (ADR 0022) is a SQLite database at `/data/api/.metadata/metadata.db` with
forward-only migrations recorded in `schema_migrations`. On startup the API applies any pending
migrations, each in its own transaction (a failed one rolls back and startup stops).

- **Before an upgrade that changes the schema** (the release notes or the `schema_version` in the
  `service.started` telemetry will say so), take and verify a workspace backup:
  `python -m scripts.workspace_backup backup ...` then `verify ...`
  ([local-containers.md](local-containers.md#backup-and-restore-workspace-state)).
- **Rolling back to a build with an older schema is refused.** That build stops at startup with
  "metadata schema vN is newer than this build" and leaves the database untouched. There are no
  down-migrations. To roll back across a schema change: stop the API, restore the pre-upgrade backup with
  `--replace`, then start the older build. Changes made after the backup are lost.
- A rollback across a release with no schema change needs nothing beyond the steps below.
- An unreadable database is quarantined as `metadata.db.corrupt-<UTC stamp>` and the API starts empty; the
  workspace directories are kept (the orphan sweep is skipped on that start). Restore the latest backup to
  recover, or inspect the quarantined file.

## Roll back an upgrade

Record the current and previous known-good SHAs before changing versions. Workspaces, consent, and uploads
persist across the restart; datasets, analyses, chats, and indexes (not yet durable) will be lost.

```powershell
docker compose down
git checkout <previous-tag-or-commit>
docker compose build
docker compose up -d --wait
docker compose exec api python -m scripts.verify_audit
python scripts/compose_smoke.py --compose --expect-local-only
```

If the audit verification fails, stop the API and follow the restore procedure in
[local-containers.md](local-containers.md); do not continue writing to an unverified chain. A code
rollback does not restore workspace state. Re-upload inputs after the old version is healthy.

To return temporarily to the compatibility UI while investigating a React/API regression:

```powershell
docker compose --profile streamlit up -d --wait streamlit
```

This is a surface rollback, not a data migration. Report the regression with the commit SHA,
request ID, browser console/CSP evidence, and the smallest reproducible input permitted by policy.

## pgvector document index: apply, verify, and roll back

This section applies only when `DOCUMENT_INDEX_BACKEND=pgvector` ([ADR 0018](../adr/0018-pgvector-document-index.md)). The default SQLite + Chroma install has no
migrations; its data is process-local workspace directories plus the audit log (see [local containers](local-containers.md)).

### Apply

Credentials come from your environment only; never commit a DSN.

```powershell
pip install -r requirements-postgres.txt
$env:POSTGRES_DSN = "postgresql://migrator:...@127.0.0.1:5432/bi"   # a role allowed to CREATE EXTENSION vector
python -m scripts.migrate_postgres            # applies pending migrations; safe to repeat
python -m scripts.migrate_postgres --verify   # read-only; exit 1 if the schema is not exactly current
```

Applying takes a transaction-scoped advisory lock, so two processes starting together cannot race, and each run is one transaction: a failing migration rolls back and
records nothing. The application itself only *verifies* at startup (or applies if you set `POSTGRES_AUTO_MIGRATE=true`, which is not recommended for shared
environments). Run the application as a role without `CREATE EXTENSION` rights.

### What startup refuses

| Situation | Behavior |
|---|---|
| Schema missing or behind | Startup fails with a message pointing at `scripts.migrate_postgres` |
| Schema **newer** than the code | Startup fails; apply refuses to touch it. Upgrade the application, do not downgrade the schema blind |
| Backend selected without a `postgresql://` DSN in the environment | Startup fails closed |

### Back up and restore

The index is derived data: every row can be rebuilt by re-uploading the PDFs. Back up when rebuilding is not acceptable, using your PostgreSQL tooling (the Compose
`ops/` scripts cover the audit log only):

```powershell
pg_dump --format=custom --table=document_chunks --table=schema_migrations "$env:POSTGRES_DSN" --file bi-index.dump
pg_restore --clean --if-exists --dbname "$env:POSTGRES_DSN" bi-index.dump
python -m scripts.migrate_postgres --verify
```

### Roll back

1. **Stop the new application version** (nothing else writes the index).
2. If the previous application version expects an older schema, restore the dump taken *before* the upgrade (it restores `schema_migrations` too), then run
   `python -m scripts.migrate_postgres --verify` with the **old** release: it must report current.
3. If no dump exists, delete the index and rebuild: drop `document_chunks` and `schema_migrations`, run the old release's `scripts.migrate_postgres`, and have users
   re-upload documents. Chunks are derived; nothing else is lost.
4. To go back to the default backend instead, set `DOCUMENT_INDEX_BACKEND=chroma` (or unset it) and restart. Workspaces are process-local, so documents are re-indexed on upload.

There are no down-migrations by design: a schema newer than the code is refused rather than reversed, which makes the restore-or-rebuild path above the supported rollback.

### Verify in CI

The `pgvector` CI job applies the migration twice (idempotence), verifies it, and runs the shared document-index contract and retrieval-metric parity against a service
container. Locally those cases are skipped with an explicit reason unless `PGVECTOR_TEST_DSN` is set to a disposable database.
