# Brief: #17b Complete long-running HTTP semantics

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/17
Tier: medium. Check mode: --full.

## Outcome

Complete the API contract begun in #17a: paginate the remaining collection, expose bounded failed-job
retry, stream downloads with explicit transfer headers, and document idempotency and timeout/retry
behavior for every create operation so issue #17 can close.

## Scope

- Reuse one opaque `(created_at, id)` cursor implementation for jobs and conversation messages.
- Add `POST /api/v1/jobs/{job_id}/retry` for failed owned jobs with retry capacity.
- Stream report/export bytes in bounded chunks with explicit length, disposition, caching, range, and
  disconnect semantics.
- Put create-operation idempotency and timeout rules in both OpenAPI descriptions and narrative docs.
- Cover pagination boundaries, retry conflict/isolation, duplicate creates, download headers/chunks,
  and reproducible additive OpenAPI.

## Decisions

- Existing v1 create operations stay synchronous. This issue specifies their behavior; it does not
  silently change response types. Any future async alternative must be additive and use the job
  admission contract.
- Only workspace creation currently supports `Idempotency-Key`. Every other create explicitly says
  that automatic retry is unsafe; unsupported keys are not implied to work.
- Downloads do not support ranges. A disconnect cancels only that transfer and a later full `GET` is
  safe.

## Acceptance

- [x] Jobs and messages use stable, bounded opaque cursor pagination.
- [x] Failed-job retry is tenant-owned, bounded, content-free, and conflict-safe.
- [x] Report/export downloads declare and send exact transfer headers in bounded chunks.
- [x] Every create path states scope, retention, replay, conflict, timeout, and retry semantics.
- [x] Contract and integration tests cover cancellation, disconnects, duplicates, and boundaries.
- [x] OpenAPI remains additive and reproducible.

## Do not touch

- No worker, broker, persistence, new API version, prompt/evaluation change, or frontend UX redesign.
- Do not expose job results, closures, exception text, idempotency keys, or user content.
