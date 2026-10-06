# Job HTTP contract

This is the first independently mergeable HTTP slice of issue
[#17](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/17). It exposes the
tenant-owned control plane for the in-process [job contract](job-contract.md) without changing any
existing operation's synchronous response. No analysis, indexing, report, or export operation is
job-backed yet.

## Resource and ownership

`JobResponse` contains content-free metadata only: opaque identifiers, the validated operation token,
state, progress counts, a safe error category, retry accounting, cancellation state, timestamps, and
the originating request ID. It never serializes the in-memory result, request fingerprint,
idempotency key, work closure, exception text, uploaded data, question, SQL, rows, document text, or
prompt.

Tenant identity comes from the verified server-side identity dependency. An unknown job and another
tenant's job both return the same `404 resource_not_found` response.

## Endpoints

| Request | Success | Semantics |
|---|---|---|
| `GET /api/v1/jobs?limit=50&cursor=...` | `200` | Stable ascending `(created_at, id)` order. `limit` is 1-100. `next_cursor` is opaque, exclusive, and `null` on the final page. Invalid cursors return `422 invalid_cursor`. |
| `GET /api/v1/jobs/{job_id}` | `200` | Returns current content-free state and progress. |
| `DELETE /api/v1/jobs/{job_id}` | `200` or `202` | Idempotent best-effort cancellation. A queued job becomes `cancelled` immediately. A terminal job is unchanged (`200`). A running job records `cancel_requested` and returns `202`, `Location`, and `Retry-After: 1`. |

Job list and detail responses use `Cache-Control: no-store`. Clients may close or time out a polling
request without changing job state; cancellation requires the explicit `DELETE` command. A running
handler can still succeed after cancellation if it does not reach a cooperative cancellation check.

## Client timeout and retry rules

- `GET` list/detail requests are safe to retry after a client timeout or disconnect. Retry transient
  failures with bounded exponential backoff and jitter.
- `DELETE` cancellation is idempotent and safe to retry. Poll the URL in `Location` after `202` and
  honor `Retry-After` when present.
- `401`, `403`, `404`, `409`, and `422` are not transient. Do not retry them unchanged.
- For `429` or `503`, retry only when the operation is otherwise safe and honor `Retry-After`.
- Server-side work is not cancelled merely because the initiating or polling HTTP connection closes.

## Create-operation idempotency contract

When a later slice moves an existing create operation behind jobs, it must remain additive within
`/api/v1` and use all of these rules:

1. Accept `Idempotency-Key` (1-128 characters) and scope it by tenant, workspace, and operation.
2. Hash the normalized request into a fingerprint. The same key and fingerprint replay the original
   job without rerunning work; the same key with a different fingerprint returns `409`.
3. Return `202 Accepted` with the `JobResponse`, `Location: /api/v1/jobs/{id}`, and a polling
   `Retry-After` value. Replays return the same job and location.
4. Retain the mapping until the job expires or its workspace is purged. The current process-local
   default TTL is one hour; process restart loses both job and key under ADR 0010/0020.
5. A client may retry a timed-out create request only with the same key and byte-equivalent normalized
   request. A create without a key is not safely retryable.

## Deferred issue #17 slices

- Move measured job-class operations behind the contract without changing existing clients abruptly.
- Define a public failed-job retry command and its idempotency semantics; `retryable` is informative in
  this slice and does not itself authorize a retry request.
- Add explicit `Content-Length`, `Content-Disposition`, range/disconnect behavior, and integration tests
  for report/export downloads.
- Apply the same pagination envelope to message and other collection endpoints.
- Purging jobs during workspace retention/deletion remains issue #18; persistence remains issue #12;
  telemetry/audit export of job events remains issue #15.
