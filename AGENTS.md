# Repository Guidance

## Project Purpose

This is a conversational business intelligence application for uploaded CSV,
Excel, and PDF files. It combines deterministic analytics, safe SQL querying,
document retrieval, anomaly detection, visualization, and report generation.

## Architecture

- `app.py` is the developer-only Streamlit compatibility entry point; React/FastAPI is the primary
  local product under ADR 0011.
- `apps/web/` is the React and TypeScript browser client.
- `apps/api/` is the versioned FastAPI service boundary.
- `src/ui/` contains upload, dashboard, and chat interfaces.
- `src/ingestion/` loads tabular files and PDFs.
- `src/profiling/` handles schema mapping, profiling, and capability detection.
- `src/analytics/` provides deterministic analytics and anomaly detection.
- `src/storage/` stores tabular data in SQLite and executes validated queries.
- `src/documents/` handles chunking, embeddings, ChromaDB storage, and retrieval.
- `src/agents/` contains the SQL, RAG, and reporting workflows.
- `src/orchestration/` routes questions through SQL, document RAG, memory, or fallback paths with LangGraph.
- `src/memory/` manages session-level conversational context.
- `src/reporting/` produces PDF reports.
- `packages/analytics/` exposes framework-neutral tabular commands and services.
- `packages/connectors/` defines provider and repository ports for application code.
- `packages/retrieval/` exposes framework-neutral PDF indexing.
- `packages/observability/` provides request-ID context, allowlist-only telemetry, and safe error categories.
- `packages/governance/` provides the audit vocabulary and append-only, hash-chained audit sinks.
- `packages/evaluation/` and `evals/` provide the deterministic evaluation harness, fixtures, thresholds, and baseline.
- `packages/retrieval/` exposes framework-neutral document-indexing commands and services.
- `Dockerfile.api`, `Dockerfile.web`, `compose.yaml`, and `docker/nginx/` define the local container topology;
  `ops/` holds audit backup/restore scripts.
- `tests/` contains the pytest suite.
- `compose.e2e.yaml` is test-only and mounts deterministic providers that must never be referenced
  by production images, default Compose configuration, or request-selectable code.

## Technology

- Python, Streamlit, FastAPI, React, and TypeScript
- Vite for the web build, Vitest for component tests, and Playwright for browser tests
- LangGraph for routing and orchestration
- Gemini 2.5 Flash for SQL generation and grounded document answers
- SentenceTransformers `all-MiniLM-L6-v2` for embeddings
- ChromaDB for local vector storage
- SQLite for uploaded tabular data
- Plotly and ReportLab for visualization and reports

## Development Commands

Create and activate a virtual environment before installing dependencies.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
```

Run quality checks with:

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy apps config packages scripts src tests
python -m scripts.run_evaluations
python -m scripts.check_openapi_compatibility --base-ref origin/main
```

Container changes: keep images digest-pinned, non-root, and secret-free, and update
`tests/test_container_config.py` when a hardening invariant intentionally changes. With Docker
available, `docker compose up -d --build --wait` then
`python scripts/compose_smoke.py --compose --expect-local-only` validates the stack; CI runs
the same in its `containers` job. See `docs/operations/local-containers.md`.

The evaluation command is offline and deterministic. Changing a prompt, retrieval
setting, fixture, or safety behavior requires updating `evals/v1/` and its baseline
as described in `docs/governance/evaluation.md`. Telemetry and audit attributes are
allowlists: never add a field that can carry a question, SQL, result rows, document
text, prompts, or secrets.

Run frontend quality checks from `apps/web/` with:

```powershell
npm ci
npm run generate:api
npm run lint
npm run typecheck
npm run test
npm run build
npm run test:e2e
```
