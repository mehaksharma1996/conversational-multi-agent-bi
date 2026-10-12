# Workspace recovery in the browser

## Problem

The React client used to generate a random idempotency key on every page load and create a
workspace with it, so a refresh silently started an empty workspace and orphaned the previous
dataset, documents, conversation, and any pending SQL approval. Child resources can only be fetched
by identifier, and the API offered no way to ask "which workspaces do I own?".

## Design

The server is the only authority. The browser never decides what it may see.

```text
page load
  GET /api/v1/workspaces                      -> the caller's own workspaces, newest first
  prefer the sessionStorage hint if it is in that list, else the newest
  GET /api/v1/workspaces/{id}/overview        -> ids of the latest dataset/analysis/documents/conversation
  GET dataset | analysis | document-collection | conversation | messages   (existing endpoints)
  render the restored state, or POST /workspaces only if the server said "none"
```

| Concern | Behavior |
|---|---|
| Authentication | Both new endpoints require `workspace:read` and use the same identity dependency as every other route. A signed-out or expired session gets 401 and the existing sign-in flow. |
| Tenant isolation | `GET /workspaces` filters by the caller's tenant. `overview` uses the same tenant-scoped lookup as `GET /workspaces/{id}`: a foreign or invented identifier returns exactly the same 404 as a missing one. Covered by `tests/test_api_workspace_recovery.py`. |
| Client-supplied identifier | A hint is only used to choose among workspaces the server already listed. An identifier the server did not list is never requested (asserted in the browser journey "a stored workspace identifier is only a hint"). |
| Stored data | `sessionStorage` holds one opaque workspace id and nothing else. No rows, SQL, answers, or tokens; `localStorage` is not used. Blocked storage is tolerated and recovery then relies on server discovery alone. |
| Retention | `GET /workspaces` is read-only and does not extend a workspace's life. Fetching the workspace and its resources does, as any use does. |
| Expired or deleted | A listed workspace that vanishes before it is loaded is skipped. If none is usable, the client creates a new one and, if it had a hint, says the earlier workspace is no longer available. |
| Failures | A network or server error during discovery is shown with a retry button. The client does not create a new workspace in that case, so a transient outage cannot orphan existing work. |
| Partial recovery | A resource the server dropped as unrecoverable (a corrupt upload, a changed parser limit) is treated as absent. A conversation is restored only if it still matches the current dataset and documents. |

## What survives a restart

Durable metadata (ADR 0022, `DURABLE_METADATA=true`) rebuilds the workspace, dataset, analysis,
document index, and conversation history after an API restart. The first request after a restart
does the rebuild, so recovery can take several seconds for a large table; the header shows
"Restoring or starting workspace…" meanwhile. Verified in the browser by restarting the API process
between two page loads.

**Pending SQL approvals do not survive a restart.** LangGraph checkpoints are held in the API
process's memory. After a restart the pending message is marked rejected with an explanatory answer
and nothing is executed; the composer is usable again. The interface says this next to the approval
buttons and does not imply durable approval.

Without durable metadata the process-local repository loses everything on restart. The client then
finds no workspace, creates a new one, and explains why.

## Contract additions

Additive and backward compatible (`scripts.check_openapi_compatibility` passes):

- `GET /api/v1/workspaces` returns `{items: Workspace[]}`.
- `GET /api/v1/workspaces/{workspace_id}/overview` returns the workspace plus
  `dataset_id`, `analysis_id`, `document_collection_id`, and `conversation_id` (each nullable).
- `ProvenanceResponse.execution` adds seven content-free counts taken from the existing
  `AnswerDiagnostics` (`sql_row_count`, `sql_execution_ms`, `sql_correction_attempted`,
  `retrieval_candidates`, `retrieval_accepted`, `llm_calls`, `llm_fallbacks`). It carries no
  question, SQL, rows, excerpts, prompts, or timestamps, and there is no new tracing store.

## Known limits

- Local mode has a single tenant, so any browser profile on the machine sees the same workspaces.
  With OIDC each user sees only their own.
- Multiple workspaces per tenant are listed but the interface restores one (the hinted or newest).
  There is no workspace picker.
- The server keeps result tables only for the newest `MAX_CHAT_DATAFRAMES_RETAINED` answers (default
  10; existing behavior), so older answers restored after a reload show their text and SQL without
  rows.
