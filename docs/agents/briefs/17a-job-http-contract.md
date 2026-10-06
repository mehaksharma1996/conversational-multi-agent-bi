# Brief: #17a Job HTTP control plane

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/17
Tier: medium. Check mode: --full.

## Outcome

Expose an additive, tenant-owned HTTP control plane for the process-local job contract: stable cursor
pagination, status polling, and explicit best-effort cancellation. Existing synchronous operations
and response schemas remain unchanged.

## Facts (verified against the code on 2026-10-06)

- `packages/jobs/` implements the framework-neutral state machine, bounded executor, ownership,
  idempotency, retries, expiry, and cooperative cancellation from #10b.
- No API operation is job-backed yet; measurements and candidate operations are in
  `docs/operations/request-budgets.md`.
- API identity is server-derived, errors use the safe envelope, and OpenAPI changes must be additive.

## Scope

- Add tenant-scoped `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}`, and
  `DELETE /api/v1/jobs/{id}`.
- Add stable `(created_at, id)` cursor pagination and content-free serializers.
- Compose the executor into FastAPI lifecycle/dependencies.
- Test pagination boundaries, invalid cursors, isolation, cancellation replay/best effort, polling
  disconnect behavior, and OpenAPI declarations.
- Document idempotency, client/server timeout, polling, cancellation, and retry rules.

## Do not touch

- Do not change an existing operation to return a job in this slice.
- No durable job state (#12), workspace job purge (#18), telemetry/audit mapping (#15), worker, queue,
  or message broker.
- Do not serialize job results, work closures, exception text, or user content.

## Acceptance

- [x] List and detail are tenant-owned, `no-store`, and use the standard safe errors.
- [x] Pagination is stable, bounded to 100, and rejects malformed cursors.
- [x] Cancellation is explicit, idempotent, and distinguishes terminal (`200`) from pending (`202`).
- [x] Poll disconnect does not imply cancellation.
- [x] OpenAPI is additive and reproducible.

## Deferred

Operation migration, public retry commands, streaming-download completion, and pagination of other
collections remain separate #17 slices.

## Evaluation and docs impact

No prompts, retrieval settings, fixtures, or safety decisions change; `evals/v1/` is unchanged.
Primary narrative: `docs/architecture/job-http-contract.md`.
