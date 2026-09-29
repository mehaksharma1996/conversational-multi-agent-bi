# Modernization baseline

This document establishes the Phase 1 baseline for
[issue #6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6).
It is a design and characterization deliverable; it does not introduce a new
runtime. The inspected baseline revision is
`c1f643b3361b3a762f224f0c144ae32e1b635930`.

## Current system

The current product is one Streamlit process. It creates a tenant/session-scoped
workspace, accepts tabular and PDF uploads, runs deterministic analysis, stores
tabular data in SQLite and document chunks in ChromaDB, and routes questions
through LangGraph to memory, guarded SQL, RAG, or hybrid workflows.

| Capability | Current owner | Migration requirement |
|---|---|---|
| Page composition and browser state | `app.py`, `src/ui/` | Move product UI to React; expose operations through application services and FastAPI. |
| Ingestion and profiling | `src/ingestion/`, `src/profiling/` | Preserve behavior in framework-neutral analytics services. |
| Analytics, anomalies, and charts | `src/analytics/`, `src/charts/` | Preserve deterministic calculations and serialize explicit result contracts. |
| SQL generation and enforcement | `src/agents/sql_agent.py`, `src/storage/query_executor.py` | Keep the model as a query proposer and deterministic code as the enforcement boundary. |
| PDF retrieval and grounding | `src/documents/`, `src/agents/rag_agent.py` | Preserve relevance rejection, deduplication, citations, and grounding diagnostics. |
| Routing and hybrid workflow | `src/orchestration/` | Place behind a typed conversation application service. |
| Reports and exports | `src/reporting/`, `src/ui/` | Expose report/export resources without weakening output safety. |
| Identity and isolation | `app.py`, `config/settings.py`, `src/utils/identity.py` | Move trusted identity and tenant authorization to the API boundary. |
| Retention and cleanup | `src/storage/session_cleanup.py` | Preserve heartbeat, periodic cleanup, safe deletion, and audit behavior. |

## Target system

The target is a local, cloud-neutral composition of a React web client, FastAPI
service, reusable packages, current local stores/providers, and an optional
worker behind a job interface. The target diagram is delivered as a validated
Archify artifact in the first-iteration diagram folder under `.archify/`.

The target is intentionally not a hosted-deployment design. Docker Compose is
the operational boundary for this roadmap.

## Migration invariants

### Product behavior

- Streamlit remains usable until React/FastAPI parity is demonstrated.
- CSV, Excel, PDF, analytics, anomaly, SQL, RAG, hybrid, report, and export
  workflows retain their documented behavior.
- Low-confidence mappings, anomaly caveats, generated SQL, evidence, and
  provenance warnings stay visible to users.

### Security and privacy

- Tenant context comes from verified server-side identity, never a client claim.
- Every resource lookup, job, export, reset, and deletion enforces ownership.
- SQL remains read-only, schema-constrained, row-limited, and time-limited.
- Local-only mode prevents construction of a network-bound Gemini client.
- Default telemetry excludes raw user content and sensitive identifiers.
- Spreadsheet export neutralization and safe user-facing errors remain enabled.

### Data and lifecycle

- Each tenant/session retains an isolated workspace.
- Failed indexing cannot replace the last known-good Chroma collection.
- Reset and deletion cover SQLite, Chroma, uploads, temporary files, jobs, and
  cached artifacts.
- Storage versions, migrations, backups, restores, and recovery are explicit.

### Agent boundaries

- Models may classify, propose SQL, extract bounded criteria, and synthesize
  grounded answers.
- Models do not authorize access, execute unvalidated SQL, enforce retention,
  delete data, or make autonomous consequential decisions.
- Model and prompt changes are evaluated and versioned.

### Delivery quality

- Existing Python tests remain the regression baseline.
- New API, frontend, evaluation, isolation, and Compose suites become additive
  gates.
- OpenAPI and the generated TypeScript client are reproducible artifacts.

## Phase 1 outputs

- Accepted ADRs under `docs/adr/`.
- Initial API resource model in `docs/architecture/api-resource-model.md`.
- Versioned evaluation manifest under `evals/baseline/`.
- Characterization test that binds the routing cases to current behavior.
- Repository-backed target architecture candidate and interactive HTML.

## Deferred from this iteration

- Creating `apps/` or `packages/` runtime scaffolding.
- Adding FastAPI, React, Node, or worker dependencies.
- Moving existing Python modules.
- Adding Docker images or Compose configuration.
- Selecting or implementing any hosting platform.
