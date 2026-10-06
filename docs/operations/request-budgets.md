# Long-running operation measurements and request budgets

Evidence for issue [#10](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/10)
and [ADR 0020](../adr/0020-in-process-execution-and-job-contract.md). Reproduce with:

```powershell
python -m scripts.measure_limits                   # deterministic probes (no model needed)
python -m scripts.measure_limits --real-embedder   # also the SentenceTransformer probes
python -m scripts.measure_limits --only report_render --output limits.json
```

Each probe runs in a **fresh subprocess**. Peak memory is the process resident set size sampled every
20 ms while the operation runs, so native allocations (torch, Chroma) are included; the browser that
Plotly's image export launches (kaleido) is a *child process and is not counted*. "Growth" is peak
minus the resident size just before the operation. Timings are indicative and machine-dependent;
nothing here is enforced by CI. Fine-grained component timings are in [benchmarks](benchmarks.md).

## Measurements (2026-10-06, Windows 11, 12 logical CPUs, CPython 3.14.3, commit 2ff6640 + #10 fix)

| Operation (probe) | Size | Wall time | Peak RSS | RSS growth |
|---|---|---:|---:|---:|
| Analysis request: load, profile, analyze | 100,000 rows (5 MB CSV) | 5.1 s | 232 MB | 49 MB |
| | 500,000 rows (25 MB CSV) | 24.6 s | 403 MB | 200 MB |
| | 1,000,000 rows (the `MAX_TABULAR_ROWS` default) | about 50 s *(linear extrapolation)* | about 600 MB *(extrapolation)* | about 400 MB *(extrapolation)* |
| Supervised classifier on a confirmed label (runs inside analyze) | 50,000 rows | 7.0 s | 259 MB | 69 MB |
| PDF report **with** 7 chart images | 7 charts, 184 KB PDF | 18.0 s cold, 16.3 s warm | 233 MB | 19 MB |
| PDF report without charts | same report | 0.04 s | n/a | n/a |
| PDF index, hashing embedder (isolates this repository's code) | 500 pages, 1,033 chunks | 2.7 s | 345 MB | 90 MB |
| PDF index, **real** all-MiniLM-L6-v2 on CPU | 60 pages, 124 chunks | 2.7 s | 793 MB | 134 MB |
| | 500 pages, 1,033 chunks (`MAX_PDF_PAGES` default) | 20.7 s | 834 MB | 171 MB |
| Embedding model cold load (first index in a process) | n/a | 6.6-10.9 s | included above | n/a |

Analysis stage split at 500,000 rows: load 2.7 s, profile 11.7 s, analyze 10.2 s. Profile cost is
dominated by whole-column date detection (see [benchmarks](benchmarks.md) finding 1).
SQL execution is bounded by `max_rows=500` and measured at 12-144 ms for 100,000 rows; it is not a
long-running operation. The classifier probe uses a synthetic label confirmed by override, as the
product requires.

## Findings

1. **Report export with charts is the slowest operation relative to its size**, and its cost is
   independent of dataset size: about 2.3 s per chart for the Plotly/kaleido image export. It runs inside
   `GET /api/v1/reports/{id}/content?format=pdf`, a synchronous handler, so it occupies one worker
   thread for 16-18 s. Chart images are the cost; the report text alone renders in 40 ms.
2. **Analysis scales linearly at about 50 microseconds per row** (profile roughly equals analyze),
   reaching about 25 s at 500,000 rows and an extrapolated 50 s at the 1,000,000-row default limit.
   Memory grows about 0.4 MB per 1,000 rows.
3. **PDF indexing with the real model costs about 20 ms per chunk on CPU** (20.7 s for 1,033 chunks) and
   the process holds about 0.8 GB once the model is resident; the model dominates peak memory, and the
   7-11 s cold load is paid by the first index in each process. The deterministic part (extract, chunk,
   store) is about 3 s for 500 pages.
4. **An `async` route ran indexing on the event loop.** `create_document_collection` called the
   blocking `service.index(...)` directly, so one index froze every other request for its whole
   duration (the 20.7 s above). The container healthcheck probes `/health/ready` with a 4 s timeout
   and nginx proxies health with a 10 s read timeout, so a long index could make a healthy API look
   dead. Fixed in this slice by running the call in the threadpool; the regression test
   `tests/test_event_loop_responsiveness.py` blocks inside the embedder and requires liveness to be
   answered while indexing is still in progress (it failed with a 5.4 s stall before the fix).
5. **Concurrency is unbounded per process.** Rate limits (#31a) are per tenant/workspace and the
   threadpool allows up to 40 concurrent sync handlers. N simultaneous 1,000,000-row analyses would
   each hold roughly 400 MB, and the compose file sets no memory limit.

## Proposed request budgets

Budgets are for a single local node at the *documented default limits*. They are targets that the
job contract (#10b) and later work are designed against, not enforced gates.

| Class | Budget (wall time) | Operations today |
|---|---|---|
| Interactive | at most 2 s compute (excluding model calls) | guarded SQL, retrieval, session/consent/lifecycle, report without charts |
| Bounded request | at most 30 s at default limits, memory at most 1 GB per operation | analysis up to about 500k rows, PDF index up to 500 pages (20.7 s, 834 MB with the real model) |
| Job-class (should not hold an HTTP request open by contract) | anything expected above 30 s | report export with charts (16-18 s today, grows with chart count), analysis near the 1,000,000-row limit (about 50 s), cold-start indexing (model load plus index, about 30 s) |

The proxy allows 300 s per API request, so nothing measured here *fails* today; the budgets exist so
that clients can be told up front which operations may be polled instead of awaited.

## Not measured (and why)

- Chromium memory used by kaleido (a child process outside the sampler); the 233 MB figure for report
  rendering excludes it.
- Concurrent mixed load. The event-loop regression test covers the one contention defect found; a
  load test belongs with the job/concurrency-cap work.
- pgvector indexing latency (needs a database; CI exercises correctness only).
- Model-provider (Gemini) latency, which is outside this repository's control.
