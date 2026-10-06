# Job contract

Defined by [ADR 0020](../adr/0020-in-process-execution-and-job-contract.md) for issue
[#10](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/10). Implemented in
`packages/jobs/` (framework-neutral: no FastAPI, Streamlit, or `apps` imports) with a process-local store and a
bounded in-process executor. The additive [HTTP control plane](job-http-contract.md) exposes tenant-owned
listing, polling, and cancellation, but **no API operation is job-backed yet**. Operation migration and the
remaining HTTP semantics belong to [#17](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/17),
and durable state to [#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12).
Which operations should move first is in [request budgets](../operations/request-budgets.md).

## Record

| Field | Meaning |
|---|---|
| `id` | Opaque, server-generated identifier |
| `tenant_id`, `workspace_id` | **Ownership.** Every store and executor call takes the tenant; another tenant's job is indistinguishable from a missing one (`JobNotFoundError`) |
| `operation` | Validated lowercase token such as `documents.index` (never free text) |
| `status` | `queued`, `running`, `succeeded`, `failed`, `cancelled`, `expired` |
| `progress_stage`, `progress_completed`, `progress_total` | Stage token plus counts; `0 <= completed <= total` |
| `error_category` | Value from the shared safe vocabulary (`packages/observability/errors.py`); the exception message is never stored |
| `idempotency_key`, `request_fingerprint` | Replay scope key and a digest of the request (see below) |
| `attempt`, `max_attempts` | Retry accounting; `attempt` starts at 1 |
| `created_at`, `updated_at`, `expires_at` | Lifecycle timestamps (injectable clock) |
| `cancel_requested` | Cooperative cancellation flag |
| `request_id` | Correlation ID of the originating request, bound inside the handler so telemetry and audit emitted by the work carry it |
| `result` | In-memory only; excluded from `repr` and equality; cleared on expiry |

## State machine

```
queued -> running -> succeeded
   |         |-----> failed ----retry (attempt+1, new attempt starts clean)---> queued
   |         '-----> cancelled
   '-> cancelled          succeeded | failed | cancelled -> expired   (expired is final)
```

`failed` is the only terminal state that can leave terminal position, and only to be retried while
`attempt < max_attempts`. Any other transition raises `JobConflictError`.

## Semantics

- **Idempotency.** Scope is (tenant, workspace, operation, key). A replay with the same fingerprint returns
  the existing job and runs nothing; a different fingerprint is a conflict. Replays do not consume capacity.
  Keys of purged or expired jobs are released.
- **Expiry.** `expires_at = created_at + ttl`. A lapsed job becomes `expired` and its result is cleared the
  next time it is read; `purge_expired()` removes it. Running jobs are not expired under their handler.
- **Retry.** Only `failed` jobs, within `max_attempts`; the work closure is kept for that purpose and
  released when retries are exhausted, the job is cancelled or succeeds, or it expires or is purged.
- **Cancellation (best effort, explicit).** A `queued` job is cancelled immediately and never runs. A
  `running` job gets `cancel_requested`; the handler stops at its next `context.raise_if_cancelled()` or
  `context.progress()`. A handler that never checks completes normally and the job is `succeeded`.
  Cancelling a finished job is a no-op.
- **Bounded concurrency.** `max_workers` handlers run; at most `max_queued` more wait; beyond that
  `submit` raises `JobQueueFullError` and records nothing.
- **Workspace deletion and retention expiry.** `purge_workspace(tenant, workspace)` deletes the workspace's jobs
  and work closures; a running handler observes `cancel_requested` and its late outcome is discarded. When jobs
  are wired into the API, call it from the repository lifecycle listener beside the rate-limit and embedding-cache
  purges so deletion covers jobs ([#18](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/18)).
- **Observer.** An optional callback receives content-free `JobEvent`s (IDs, operation token, status,
  attempt, request ID, error category, duration). Observer failures never affect execution.

## Invariants proved by tests

- `tests/test_jobs.py`: legal transitions only; tenant isolation; idempotent replay and conflict; bounded
  admission and concurrency; queued and cooperative cancellation; safe failure category (no message leaks);
  retry budget; expiry; workspace purge; event ordering; a repeated destructive job runs once.
- `tests/test_job_index_retry.py`: a failed index job (provider outage after the first embedding batch)
  leaves the previous index fully published with no staging collection, and a retry replaces it exactly
  once; a workspace-deletion job and its replay or retry delete the workspace exactly once.

## Privacy

Records, events, and logs contain identifiers, tokens, counts, categories, and timings only. Work closures and
results may hold user content, live only in process memory, and are dropped as described above. Nothing here
is persisted.
