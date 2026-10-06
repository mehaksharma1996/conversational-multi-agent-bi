# Long-running client HTTP contract

This completes the long-running-client semantics of issue
[#17](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/17). It defines the
tenant-owned control plane for the in-process [job contract](job-contract.md), stable collection
pagination, streamed downloads, and the retry/idempotency rules of every v1 operation. Existing v1
creates remain synchronous and retain their response types; a future asynchronous alternative must
be additive and use this job contract.

## Job resource and ownership

`JobResponse` contains content-free metadata only: opaque identifiers, the validated operation token,
state, progress counts, a safe error category, retry accounting, cancellation state, timestamps, and
the originating request ID. It never serializes the in-memory result, request fingerprint,
idempotency key, work closure, exception text, uploaded data, question, SQL, rows, document text, or
prompt.

Tenant identity comes from the verified server-side identity dependency. An unknown job and another
tenant's job both return the same `404 resource_not_found` response.

| Request | Success | Semantics |
|---|---|---|
| `GET /api/v1/jobs?limit=50&cursor=...` | `200` | Stable ascending `(created_at, id)` order. `limit` is 1-100. `next_cursor` is opaque, exclusive, and `null` on the final page. Invalid cursors return `422 invalid_cursor`. |
| `GET /api/v1/jobs/{job_id}` | `200` | Returns current content-free state and progress. |
| `DELETE /api/v1/jobs/{job_id}` | `200` or `202` | Idempotent best-effort cancellation. A queued job becomes `cancelled` immediately. A terminal job is unchanged (`200`). A running job records `cancel_requested` and returns `202`, `Location`, and `Retry-After: 1`. |
| `POST /api/v1/jobs/{job_id}/retry` | `202` | Starts exactly one new attempt only for a failed owned job with retry capacity. Other states/exhaustion return `409 job_not_retryable`; full capacity returns `503 job_queue_full` with `Retry-After`. |

Job list and detail responses use `Cache-Control: no-store`. Clients may close or time out a polling
request without changing job state; cancellation requires the explicit `DELETE` command. A running
handler can still succeed after cancellation if it does not reach a cooperative cancellation check.

Before retrying a timed-out retry command, poll the job. If its attempt advanced or it is queued or
running, do not send the command again. A repeated accepted command cannot start another concurrent
attempt and returns `409`.

## Collection pagination

`GET /jobs` and `GET /conversations/{conversation_id}/messages` use the same additive envelope: the
existing item field (`items` or `messages`) plus `next_cursor`. `limit` defaults to 50 and is bounded
to 1-100. Ordering is ascending `(created_at, id)`; the opaque cursor is exclusive. Newer records may
appear on later pages, while removed or expired records are skipped. A cursor must only be reused with
its originating collection and tenant. Invalid cursors return `422 invalid_cursor`.

## Job-backed create contract

If a future additive operation admits a job, it must use all of these rules:

1. Accept `Idempotency-Key` (1-128 characters) and scope it by tenant, workspace, and operation.
2. Hash the normalized request into a fingerprint. The same key and fingerprint replay the original
   job without rerunning work; the same key with a different fingerprint returns `409`.
3. Return `202 Accepted` with the `JobResponse`, `Location: /api/v1/jobs/{id}`, and a polling
   `Retry-After` value. Replays return the same job and location.
4. Retain the mapping until the job expires or its workspace is purged. The current process-local
   default TTL is one hour; process restart loses both job and key under ADR 0010/0020.
5. A client may retry a timed-out create request only with the same key and byte-equivalent normalized
   request. A create without a key is not safely retryable.

## Current create-operation idempotency

| Create request | Idempotency/replay | Retention and conflict |
|---|---|---|
| `POST /workspaces` | Optional `Idempotency-Key`, scoped to tenant. Same key replays the original `201`; a timed-out request is safely retried only with that key. | Mapping lasts until workspace expiry/deletion or process restart. There is no client payload, so same-key payload conflict is not applicable. |
| `POST /workspaces/{id}/tabular-uploads` | Key unsupported; duplicates may create another upload. | Child follows workspace lifetime; validation/size failures are not replay conflicts. |
| `POST /tabular-uploads/{id}/dataset` | Key unsupported; duplicate parsing/profiling may create another dataset. | Child follows workspace lifetime; source/state conflicts return `409`/`422`. |
| `POST /datasets/{id}/analyses` | Key unsupported; duplicate work may create another analysis. | Child follows workspace lifetime; unconfirmed mapping returns `409`. |
| `POST /workspaces/{id}/document-collections` | Key unsupported; duplicate extraction/embedding may repeat work and publish another resource. | Child follows workspace lifetime; limits/validation fail before publication. Index publication remains atomic. |
| `POST /workspaces/{id}/conversations` | Key unsupported; duplicates create distinct conversations. | Child follows workspace lifetime; invalid context returns `422`. |
| `POST /conversations/{id}/messages` | Key unsupported; duplicates may repeat model/SQL work and create another message. | Bounded conversation retention applies; consent/pending-approval state returns `409`. |
| `POST /analyses/{id}/reports` | Key unsupported; duplicates create distinct report resources. | Child follows workspace lifetime. |
| `POST /messages/{id}/exports` | Key unsupported; duplicates create distinct sanitized exports. | Child follows workspace/message retention; unavailable results return `409`. |

For every unsupported-key create, a timeout or disconnect does not cancel server work and automatic
retry is unsafe. The client should resolve the original outcome through its owning collection when
possible or ask the user before resubmitting. These rules are embedded in each OpenAPI operation.

## Download streaming

Report and export content responses yield bounded 64 KiB chunks and include exact `Content-Length`,
attachment `Content-Disposition`, `Cache-Control: private, no-store`, and `Accept-Ranges: none`.
Range requests are not supported and receive the complete `200` representation. A disconnect stops
only the transfer; the immutable report/export resource remains available, and a new full `GET` is
safe. Report bytes are rendered before streaming, so disconnecting cannot be used as compute
cancellation; only job work uses the explicit job cancellation command.

## Operation timeout and safe-retry matrix

| Operation class | Server behavior | Client timeout/retry rule |
|---|---|---|
| Metadata/detail and paginated `GET` | Short, side-effect free; list cursors are exclusive and stable. | Safe to retry with bounded backoff on transient `429`/`503`; honor `Retry-After`. |
| Report/export content `GET` | Representation is built/read, then streamed in bounded chunks; proxy ceiling remains 300 s. | Safe to restart from byte zero. Ranges/resume are unsupported. |
| Workspace create | Synchronous and process-local. | Safe only with the same non-empty idempotency key; without one, do not retry automatically. |
| Other create `POST`s | Synchronous; measured heavy work stays off the event loop. | Not safely retryable after an unknown outcome. Use resource lookup/user confirmation. |
| Consent/schema `PUT` and SQL approval `POST` | State/precondition checked on each request. | Do not automatically retry an unknown outcome; read current state first. |
| Workspace `DELETE` | Synchronous deletion; a repeat may return `404` after the desired state is reached. | Retry only when the user still intends deletion; treat confirmed absence as success at the workflow level. |
| Job cancel `DELETE` | Explicit, idempotent best effort. | Safe to retry; poll `Location` after `202`. |
| Job retry `POST` | One new attempt from `failed` only. | Poll first after timeout; retry command only while still failed/retryable. |

Validation/auth/ownership/state failures (`400`-class other than `429`) are not transient. Server
`500` is an unknown outcome for mutations and is not automatically retried. `429`/`503` may be
retried only when the operation is otherwise safe.

## Execution boundary decision

The current v1 endpoints stay synchronous to preserve the shipped client contract. PDF indexing is
offloaded from the event loop; analysis/indexing limits and the 300-second proxy ceiling are explicit
in [request budgets](../operations/request-budgets.md). This is a contract decision, not an implicit
promise that a disconnected request is cancelled. If later measurements require asynchronous
admission, add a new endpoint or explicit negotiation that returns `202` and the job resource; do not
change a current `201` response conditionally.

Purging jobs during workspace retention/deletion remains issue #18; persistence remains issue #12;
telemetry/audit export of job events remains issue #15.
