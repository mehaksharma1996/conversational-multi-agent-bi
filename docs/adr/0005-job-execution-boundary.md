# ADR 0005: Job execution boundary

- Status: Accepted, implementation deferred
- Date: 2026-09-28
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6)

## Context

PDF extraction, embeddings, deterministic analysis, and PDF rendering currently
run in the Streamlit process. A separate worker would add operational cost before
we know which operations need it, but the HTTP API must not be designed around
unbounded synchronous requests.

## Decision

Define a framework-neutral job interface and resource model before choosing a
queue or worker transport. The initial API may execute a job in-process behind
that interface when measurement shows it stays within documented latency and
memory budgets.

Introduce `apps/worker` only when at least one operation exceeds the agreed
request budget, requires durable retry across process restart, or causes unsafe
contention in the API process. The measurement and selected threshold belong in
the implementing child issue.

A job records owner, operation, status, progress, idempotency key, result/error,
creation/expiry time, retry count, and cancellation state.

## Consequences

- The API contract remains stable if execution later moves out of process.
- No queue technology is selected prematurely.
- In-process execution still needs bounded concurrency and restart behavior.

## Invariants

- Jobs are tenant-owned and never expose cross-tenant results.
- Retries cannot publish a partial Chroma index or duplicate a destructive
  lifecycle operation.
- Cancellation is best effort and its semantics are explicit per operation.
