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
exports, SQLite tables, or Chroma indexes. API metadata is process-local and a restart loses the
workspace even though audit/model volumes persist (ADR 0010).

## Run both surfaces side by side

```powershell
docker compose up -d --build --wait
docker compose --profile streamlit up -d --wait streamlit
```

Use React at `127.0.0.1:8080` and Streamlit at `127.0.0.1:8501`. They use distinct workspace roots.
Do not treat a result in one surface as proof that the other is operating on the same dataset.
Streamlit remains for diagnosis and compatibility comparison, not for new product features.

## Roll back an upgrade

Record the current and previous known-good SHAs before changing versions. Active API work will be
lost when the API restarts.

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
