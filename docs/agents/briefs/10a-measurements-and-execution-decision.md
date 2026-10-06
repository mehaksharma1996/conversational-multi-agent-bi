# Brief: #10a Measurements, request budgets, and the execution decision

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/10
Tier: medium. Check mode: --full.

## Outcome

Latency and peak-memory measurements of the long-running operations exist and are reproducible, request
budgets and an evidence-based in-process-vs-worker decision are recorded, and the one demonstrated
contention defect (indexing on the event loop) is fixed with a regression test. This is the first slice
of #10; it does not close it.

## Facts (verified against the code on 2026-10-06)

- `apps/api/feature_routes.py:create_document_collection` is an `async` route; before this slice it called
  the blocking `service.index(...)` directly on the event loop.
- Container healthcheck timeout is 4 s (`Dockerfile.api`); nginx proxies API reads for 300 s and health for 10 s.
- `scripts/measure_limits.py` runs each probe in a fresh subprocess and samples OS resident memory.
- `scripts/run_benchmarks.py` (#31b) holds component timings; `docs/operations/request-budgets.md` holds the
  limit-size measurements and proposed budgets; ADR 0020 records the decision.

## Do not touch

- HTTP/OpenAPI surface (jobs, polling, idempotency, cancellation are #17), queue or worker technology.
- Telemetry/audit allowlists; measurement output contains sizes, timings, and memory only.
- `profile_dataframe` behavior and chart-rendering behavior (findings are recorded, not changed).

## Acceptance

- [x] Reproducible latency and memory measurements establish request budgets (`measure_limits`, request-budgets doc).
- [x] An ADR records that execution stays in-process, with evidence and explicit worker triggers (ADR 0020).
- [x] The event-loop contention defect is fixed and regression-tested.
- [ ] Job contract covering ownership, state, progress, result/error, idempotency, expiry, retry, cancellation,
      and correlation IDs (slice #10b).
- [ ] Failure/retry tests proving no partial index publication and no duplicate destructive lifecycle action (#10b).

## Evaluation and docs impact

`evals/v1/` and its baseline are unchanged. Docs: request-budgets, ADR 0020 and the ADR index, benchmarks cross-link.

## Design decisions already made

- Subprocess-per-probe so peak RSS is attributable; real-embedder probes are opt-in and never run in CI.
- Budgets are design targets, never CI gates.
