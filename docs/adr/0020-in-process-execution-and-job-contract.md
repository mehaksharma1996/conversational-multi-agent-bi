# ADR 0020: In-process execution with a bounded job contract; worker deferred

- Status: Accepted
- Date: 2026-10-06
- Issue: [#10](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/10)
- Refines [ADR 0005](0005-job-execution-boundary.md), which required measurements before a worker. Evidence:
  [request budgets](../operations/request-budgets.md). Idempotency, cancellation, pagination, and
  streaming semantics remain with [#17](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/17).

## Context

ADR 0005 allows in-process execution while it "stays within documented latency and memory budgets", and
requires a worker only when an operation exceeds the request budget, needs durable retry across
restart, or causes unsafe contention in the API process. Measurements on the default limits show:
indexing 500 pages with the real model takes about 21 s and 0.8 GB; analysis takes about 25 s at
500,000 rows (about 50 s extrapolated at the 1,000,000-row limit); a PDF report with chart images takes
16-18 s because of image export. All finish within the proxy's 300 s, but the document-indexing route
executed on the event loop and froze the whole process for that time (a demonstrated
unsafe-contention defect).

## Decision

1. **Execution stays in-process; no `apps/worker` and no queue technology is introduced.** No measured
   operation needs durable retry across a restart (workspace state is not durable under ADR 0010), and
   the contention defect is removable without a worker. Everything is local-first and single-node.
2. **Blocking work never runs on the event loop.** Handlers that call blocking services must be
   synchronous (`def`, run by the framework in its threadpool) or offload with `run_in_threadpool`.
   Document indexing was changed accordingly and is covered by a regression test.
3. **Request budgets** are the three classes in the request-budget document (interactive at most 2 s,
   bounded request at most 30 s and 1 GB, job-class above that). They are design targets, not CI gates.
4. **Define the job contract before any operation is made asynchronous** (slice #10b), as a
   framework-neutral package executed by a bounded in-process executor: tenant-owned records with owner,
   operation, status (`queued`, `running`, `succeeded`, `failed`, `cancelled`, `expired`), progress,
   result or safe error, an idempotency scope with a request fingerprint, creation and expiry times,
   retry count, cancellation state, and the originating request ID for correlation. Terminal states are final; a retry creates a new attempt of
   the same job only from `failed`; cancellation is best effort and explicit per operation. Execution
   concurrency is capped globally so memory is bounded. The contract must not carry user content
   (questions, rows, document text) in records, telemetry, or audit.
5. **Retry and lifecycle invariants** (proved by tests in #10b): a retried or failed index never leaves
   a partially published Chroma/pgvector index, and a retried destructive lifecycle action (workspace
   deletion, expiry purge) never runs twice.
6. **A worker becomes justified, with a new ADR**, when any of these is measured: (a) an operation
   misses the bounded-request budget at *default* limits after the cheap fixes below; (b) a deployment
   requires retry that survives a process restart; or (c) with the threadpool fix and a concurrency cap
   in place, liveness probes still exceed their timeout under realistic load. A worker must stay
   optional, local-first, and cloud-neutral.

## Cheap fixes preferred over a worker (not done here)

Sample-based date detection in profiling (profiling is about half of analysis time, mostly this); rendering report charts with
fewer or smaller images or one browser session instead of one per chart; a configurable cap on
concurrent heavy operations; documenting a compose memory limit sized from the measurements.

## Consequences

- The public API contract is unchanged by this decision. When an operation becomes job-backed, #17
  decides the HTTP surface (idempotency, polling, cancellation); the contract here is the internal model.
- Measured numbers are indicative for one machine; budgets are revisited when defaults or the embedding
  model change.
