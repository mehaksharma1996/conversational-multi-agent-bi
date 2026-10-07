# Brief: #15 job and data-volume gauges, alert rules

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/15
Tier: medium. Check mode: default. Decision records: [ADR 0007](../../adr/0007-observability-privacy.md).

## Outcome

`/metrics` (when `METRICS_ENABLED=true`) also exposes job queue depth, in-flight jobs, admission capacity,
and data-volume usage, and the repository ships example alert rules whose metric references are verified
by a test.

## Done

- `InProcessJobExecutor.stats()` returns `JobExecutorStats(queued, running, capacity, workers)` under the
  executor lock; `running` is counted only after a successful `RUNNING` transition and released in `finally`.
- `MetricsRegistry.register_gauge` samples at scrape time, outside the registry lock (the executor emits
  events while holding its own lock), skips a failing probe, and rejects names outside `bi_[a-z0-9_]+`.
- `apps/api/main.py` registers `bi_jobs_queued`, `bi_jobs_running`, `bi_jobs_capacity`, and
  `bi_data_volume_bytes{state}` only when metrics are enabled.
- `ops/alerts/bi-api.rules.yml` and `tests/test_alert_rules.py`.

## Do not touch

The telemetry allowlist, job identifiers as labels, tracing, Compose topology, and the OpenAPI contract.
Process memory is deliberately not exported (no `psutil`; container runtimes own limits).

## Remaining in #15

Optional digest-pinned, non-root collector Compose profile and injected-failure drills; browser-side
`traceparent` generation.
