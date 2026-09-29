# React and FastAPI feature parity

Phase 5 migrates the remaining analyst workflow to the React/FastAPI surface
without removing the Streamlit reference application. The browser can now
combine a confirmed tabular dataset with indexed PDFs, ask questions through
the existing bounded memory, SQL, RAG, and hybrid workflows, inspect retrieved
sources and generated SQL, and download reports or sanitized query results.

## Resource flow

1. A tenant-scoped workspace advertises its expiry, provider configuration,
   local-only mode, and consent state.
2. Schema confirmation persists the normalized table to a workspace-scoped
   SQLite database. Generated SQL remains read-only, allowlisted, row-limited,
   and deadline-bound through the existing query executor.
3. PDF uploads are bounded by per-file, combined-size, page, and chunk limits.
   Indexing uses the existing page-aware chunker, embeddings, Chroma store,
   relevance filtering, and atomic collection replacement.
4. A conversation references the selected dataset and/or document collection.
   The existing LangGraph orchestrator chooses memory, SQL, RAG, hybrid, or
   unsupported routes. Responses expose the selected route, generated SQL,
   bounded rows, and document citations for review.
5. Reports are generated from deterministic analysis results. Query-result
   CSV and Excel exports pass through spreadsheet-formula neutralization.
6. Reset deletes the complete workspace graph and its local SQLite/Chroma
   directory before creating a new workspace.

Resource identifiers never grant authority. Every workspace, document,
conversation, message, report, and export lookup derives the tenant from the
trusted FastAPI identity dependency and returns not-found across tenant
boundaries.

## Consent and local-only behavior

When Gemini is configured, the API rejects model-backed questions until the
workspace records acceptance of the versioned data-sharing notice. The React
client shows that notice before enabling its question composer. `LOCAL_ONLY_MODE`
continues to disable Gemini-backed routes even if a key is present; deterministic
analysis, reports, and compatible session-memory responses remain local.

This consent is a product guardrail, not a substitute for organizational data
classification or provider agreements. Existing PII redaction and reduced
sample-value settings remain in the model boundary.

## Retention model

The API repository is still intentionally process-local. Workspace activity
extends a sliding `SESSION_RETENTION_HOURS` deadline. An access after expiry
removes the complete resource graph and workspace directory; explicit reset
does the same immediately. Chat text is bounded by `MAX_CHAT_MESSAGES`, while
only the newest `MAX_CHAT_DATAFRAMES_RETAINED` messages keep exportable frames.

There is not yet a background API reaper or durable resource catalog. An API
restart forgets its resource metadata and can leave its previously written
`APP_DATA_DIR/api` files for manual removal. Durable storage, startup
reconciliation, and asynchronous job recovery remain future work.

## API surface

- `PUT /api/v1/workspaces/{workspace_id}/consent`
- `DELETE /api/v1/workspaces/{workspace_id}`
- `POST /api/v1/workspaces/{workspace_id}/document-collections`
- `GET /api/v1/document-collections/{collection_id}`
- `POST /api/v1/workspaces/{workspace_id}/conversations`
- `GET /api/v1/conversations/{conversation_id}`
- `POST|GET /api/v1/conversations/{conversation_id}/messages`
- `POST /api/v1/analyses/{analysis_id}/reports`
- `GET /api/v1/reports/{report_id}/content`
- `POST /api/v1/messages/{message_id}/exports`
- `GET /api/v1/exports/{export_id}/content`

The committed OpenAPI document defines the JSON and binary download contracts;
the React client generates its request types from that artifact.

## Verification

Backend integration coverage exercises consent rejection and acceptance, RAG
and hybrid routing, safe SQL execution, citations, spreadsheet-safe export,
Markdown/PDF reports, cross-tenant denial, expiry, and complete reset. Vitest
covers the browser state transitions, and Playwright covers both the tabular
analysis journey and PDF-to-cited-answer journey against contract-shaped API
responses.

## Deferred concerns

- Durable repository metadata and restart recovery.
- Background workers, progress reporting, and cancellation for long indexing
  or report jobs.
- OIDC identity at the FastAPI boundary; it currently uses the explicit local
  development identity adapter.
- OCR for scanned PDFs and vector-store encryption.
- Real-model answer-quality evaluation and a production telemetry exporter.
  Phase 6 added the offline evaluation suite, structured telemetry, audit
  events, and provenance display; see
  [evaluation, governance, and observability](evaluation-governance-observability.md).
