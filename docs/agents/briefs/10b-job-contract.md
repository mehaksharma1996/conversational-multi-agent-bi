# Brief: #10b Job contract and retry/lifecycle invariants

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/10
Tier: medium. Check mode: --full.

## Outcome

A framework-neutral job contract with a bounded in-process executor exists in `packages/jobs/`, and tests
prove a failed or retried index never publishes a partial index and a destructive lifecycle action never
runs twice. This completes #10 together with #10a.

## Facts (verified against the code on 2026-10-06)

- `packages/jobs/` has `contract.py` (records, states, errors), `store.py` (tenant-scoped in-memory store),
  `executor.py` (bounded executor, context, observer).
- `packages/observability/errors.py:error_category` supplies the safe failure vocabulary reused here.
- `ChromaDocumentStore.replace_chunks` embeds before touching the live collection, so a failed index keeps the
  previous generation; the jobs tests prove it through the job boundary.
- No API route is job-backed; `apps/api` is untouched.

## Do not touch

- HTTP/OpenAPI surface and idempotency headers (#17), durable persistence (#12), worker or queue technology.
- Telemetry/audit allowlists (the observer is not wired to them yet).

## Acceptance

- [x] Job contract covers ownership, state, progress, result/error, idempotency, expiry, retry, cancellation,
      and correlation IDs (`docs/architecture/job-contract.md`).
- [x] Failure/retry tests prove no partial index publication and no duplicate destructive lifecycle action.
- [x] Any worker remains optional: none was added; ADR 0020 records the triggers.
- [x] Reproducible latency/memory measurements and the ADR (slice #10a, PR #58).

## Deferred (explicit)

Wiring operations (report export with charts, large analyses) to jobs, the HTTP surface, job purge on the
repository lifecycle listener, telemetry/audit mapping of `JobEvent`, and a configurable global concurrency cap.

## Evaluation and docs impact

`evals/v1/` and baseline unchanged. Docs: `docs/architecture/job-contract.md`.
