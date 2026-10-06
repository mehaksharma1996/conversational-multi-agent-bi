# Repository Guidance

## Codex Five-Hour Usage Guardrail

- Use the account-level five-hour **Usage remaining** percentage as the control signal. The weekly
  percentage does not trigger this guardrail unless the user explicitly says otherwise.
- Check the five-hour percentage before starting material issue work and again after substantial
  implementation or verification milestones. Use an available first-party usage-status capability.
  If the percentage cannot be read programmatically, tell the user and ask for the current five-hour
  percentage before starting or continuing substantial changes.
- Above 15% remaining, work normally.
- At or below 15% remaining, do not start a new issue or substantial subtask. Finish the current
  atomic operation and prepare to wrap up.
- At or below 10% remaining, make no additional tracked-file changes, including code, tests,
  documentation, configuration, generated files, dependency files, or formatting changes. Only:
  1. run appropriate verification that does not intentionally modify tracked files;
  2. review and commit the work already completed;
  3. push the branch to the repository; and
  4. provide a complete Claude Code handoff prompt containing the objective, branch and commit,
     completed changes, verification results, known failures or risks, remaining work, and exact
     recommended next steps.
- Git commit and push operations and writing the handoff response are allowed after the 10% cutoff;
  new implementation changes are not.

## Project Purpose

This is a conversational business intelligence application for uploaded CSV,
Excel, and PDF files. It combines deterministic analytics, safe SQL querying,
document retrieval, anomaly detection, visualization, and report generation.

## Architecture

- `app.py` is the developer-only Streamlit compatibility entry point; React/FastAPI is the primary
  local product under ADR 0011.
- `apps/web/` is the React and TypeScript browser client.
- `apps/api/` is the versioned FastAPI service boundary.
- `apps/mcp/` is the read-only stdio MCP server over the guarded SQL and retrieval core (ADR 0014); it is never imported by `src/` or `packages/`.
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

Indicative benchmarks (never a gate; see `docs/operations/benchmarks.md`):
`python -m scripts.run_benchmarks [--quick] [--only <group>] [--profile <case>]`.

Prefer `python -m scripts.check_all` (add `--full` for API or web changes, `--only <name>` to rerun
one check): it prints one PASS/FAIL line per check and only the tail of a failure. For a roadmap
issue, read its brief in `docs/agents/briefs/` before exploring; see `docs/agents/README.md`.

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
