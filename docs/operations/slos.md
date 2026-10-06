# Service level indicators, objectives, and operator queries

Part of issue [#15](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/15).
These are objectives for a single local node, not contractual guarantees. Latency objectives reuse the
measured budget classes in [request-budgets.md](request-budgets.md); error and privacy objectives follow
from [ADR 0007](../adr/0007-observability-privacy.md).

## Where the signals come from

1. **Structured telemetry** on the `conversational_bi.telemetry` logger (one JSON object per event, always on).
   Fields come from an allowlist; `request_id` joins an event to the user-visible request ID, to audit events,
   and to the error response.
2. **Metrics** in Prometheus text format at `GET /metrics`, derived from the same allowlisted events and
   **off by default**. Enable with `METRICS_ENABLED=true` (see `.env.example`). The path is outside `/api`, is
   not in the OpenAPI contract, and is not proxied by the bundled nginx image, so it is reachable only from
   wherever the API port itself is reachable. Expose it to your scraper and nobody else.

Metrics never contain questions, SQL, result rows, document text, prompts, file names, resource or tenant
identifiers, request IDs, or secrets. Label values are limited to allowlisted tokens (event names, route
*templates*, outcomes, safe error categories, answer routes, provider and model names, status classes),
enumerated labels accept only their known values, and each label is capped at 64 distinct values (excess
becomes `other`). `tests/test_metrics.py` proves each of these properties.

## Metric families

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `bi_http_requests_total` | counter | `route`, `status_class` | Requests by route template and `2xx`/`4xx`/`5xx` (health and metrics excluded) |
| `bi_event_duration_ms` | histogram | `event` | Duration of timed operations (`analysis.run`, `agent.answer`, `report.render`, `workspace.export`, ...) |
| `bi_events_total` | counter | `event`, `outcome` | Every telemetry event; `outcome` is `success`, `failure`, or `none` |
| `bi_errors_total` | counter | `event`, `error_category` | Failures by safe category (`timeout`, `unsafe_query`, ...) |
| `bi_answers_total` | counter | `route` | Answered questions by `memory`, `sql`, `rag`, `hybrid`, `unsupported` |
| `bi_llm_calls_total` | counter | `provider`, `model`, `outcome` | Model calls |
| `bi_llm_tokens_total` | counter | `provider`, `model`, `kind` | Provider-reported prompt/output tokens |
| `bi_llm_retries_total` | counter | none | Model call retries |
| `bi_jobs_total` | counter | `operation`, `status` | Job state transitions (`queued`, `running`, `succeeded`, `failed`, `cancelled`, `expired`) |
| `bi_rate_limited_total` | counter | `operation` | Requests refused by a rate limit |
| `bi_retrieval_rejected_total` | counter | none | Retrieval candidates rejected by the distance threshold |
| `bi_dropped_attributes_total` | counter | none | Attributes the allowlist rejected; should stay at zero |

## SLIs and objectives

| # | Indicator | Objective (28-day window) | Query |
|---|---|---|---|
| 1 | **API success**: share of requests that are not `5xx` | at least 99.5% | `1 - sum(rate(bi_http_requests_total{status_class="5xx"}[28d])) / sum(rate(bi_http_requests_total[28d]))` |
| 2 | **Interactive latency**: p95 of `agent.answer` for non-model routes | at most 2 s (the interactive class) | `histogram_quantile(0.95, sum by (le) (rate(bi_event_duration_ms_bucket{event="agent.answer"}[28d])))`, judged on a deployment without a hosted model; with one, provider latency dominates and is outside this repository's control |
| 3 | **Bounded-request latency**: p95 of `analysis.run` at default limits | at most 30 s (the bounded-request class) | `histogram_quantile(0.95, sum by (le) (rate(bi_event_duration_ms_bucket{event="analysis.run"}[28d])))` |
| 4 | **Model call success** | at least 95% | `1 - sum(rate(bi_llm_calls_total{outcome="failure"}[28d])) / sum(rate(bi_llm_calls_total[28d]))` |
| 5 | **Throttling**: refused requests | under 1% of requests | `sum(rate(bi_rate_limited_total[28d])) / sum(rate(bi_http_requests_total[28d]))` |
| 6 | **Privacy**: allowlist violations | exactly zero | `increase(bi_dropped_attributes_total[28d]) == 0` |
| 7 | **Job completion**: share of finished jobs that succeed | at least 99% (cancellations excluded) | `sum(rate(bi_jobs_total{status="succeeded"}[28d])) / (sum(rate(bi_jobs_total{status="succeeded"}[28d])) + sum(rate(bi_jobs_total{status="failed"}[28d])))` |
| 8 | **Job latency**: p95 of a job's final transition | at most 30 s for the operations that run as jobs | `histogram_quantile(0.95, sum by (le) (rate(bi_event_duration_ms_bucket{event="job.transition"}[28d])))` |

Objective 6 is a canary, not a performance target: a non-zero value means some code path tried to emit an
attribute that could carry content. Find it with the log query below, fix the caller, and never widen the
allowlist to silence it.

## Operator queries without a metrics store

Telemetry is JSON lines, so `jq` answers most questions. From a container host:

```powershell
# Everything that happened for one user-visible request ID
docker compose logs --no-color api | jq -c 'select(.request_id == "<id>")'

# Slowest operations in the last run
docker compose logs --no-color api | jq -c 'select(.duration_ms != null) | {event, duration_ms}' | Sort-Object

# Failures by safe category
docker compose logs --no-color api | jq -r 'select(.outcome == "failure") | "\(.event) \(.error_category)"' | Group-Object

# Allowlist violations (privacy canary)
docker compose logs --no-color api | jq -c 'select(.dropped_attributes > 0) | {event, dropped_attributes}'
```

The audit chain answers "who did what" for the same `request_id`:
`docker compose exec api python -m scripts.verify_audit`.

## Correlation across boundaries

One `X-Request-ID` is created per request (or taken from a bound context), returned to the browser, shown
in the UI on errors, stamped on every telemetry event and audit event for that request, and included in the
error body. A job's lifecycle (`job.transition` events: operation, status, attempt, duration, safe error
category) carries the `request_id` of the request that created it, so a slow or failed job is found with the
same `jq` query as any other request. Job IDs appear in the job's HTTP representation but are deliberately not
telemetry attributes or metric labels (they are unbounded identifiers).

## Not done yet (remaining in #15)

- **Trace export** (OpenTelemetry spans, OTLP). Metrics and logs are covered; spans would need an SDK
  dependency, an image-size and license review, and its own privacy tests. ADR 0015 records the deferral.
- **An optional Compose profile** that scrapes `/metrics` and demonstrates diagnosing injected failures.
  Requires a digest-pinned, non-root metrics image and loopback-only publishing.
- **Job gauges**: transitions and outcomes are exported, but there is no queue-depth or in-flight gauge; a
  gauge needs the executor to report its own state, which is a separate change.
- **Storage and resource-limit gauges** (volume usage, memory): not derived from request events.
- **Alerting rules.** The objectives above are written as queries, not as alerts.
