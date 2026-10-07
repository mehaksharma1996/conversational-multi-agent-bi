# ADR 0015: Content-free LLM call telemetry and trace-export boundary

- Status: Accepted
- Date: 2026-10-05
- Issue: [#25](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/25)
- Extends [ADR 0007](0007-observability-privacy.md); it does not change its privacy boundary.

## Context

Telemetry is request-level, allowlisted, and content-free. Model calls were visible only as one plain log
line in the Gemini client, so per-step latency, retries, token use, and cost per route could not be answered.
Issue #25 also proposes optional OpenTelemetry export and a local Langfuse/Phoenix Compose profile.

## Decisions

1. **One observation point.** `src/llm/observability.py:ObservedLLMClient` wraps any `LLMClient`. It emits
   exactly one record per call (text or structured, success or failure) and never reads prompt or response
   text. The API wraps the configured provider in `get_llm_client`; the opt-in live evaluation does the same.
   Because it is provider-neutral, a second provider (issue #27) is observed without new telemetry code.
2. **Purpose is declared by the caller**, with a context variable set at each call site
   (`route`, `sql_generation`, `sql_correction`, `rag_answer`, `criteria_extraction`). The prompt-building
   functions are not edited, so prompt fingerprints and the evaluation baseline do not change.
3. **Usage is reported by the provider client** through `report_usage`. Providers that do not report tokens
   leave them absent; scripted evaluation runs therefore keep `token_usage: null`.
4. **Cost is an operator estimate.** Two optional prices per million tokens (set together or startup fails)
   produce `llm_estimated_cost_microusd` (an integer, because telemetry floats round to three decimals).
5. **Per-answer totals** are collected in a per-question context and stored in `AnswerDiagnostics`, which
   telemetry, the provenance summary, and the evaluation harness already share.
6. **Trace export is implemented once under issue #15, not in the LLM wrapper.** The trace sink receives the
   already-sanitized `llm.call` event (purpose, provider, model, duration, counts, outcome) alongside other
   telemetry events. It never reads `LLMCallRecord`, a prompt, or a completion. A future local profile must
   stay out of the default Compose topology, loopback-only, digest-pinned, and non-root. Content-bearing
   tracing for local debugging would conflict with ADR 0007 and needs its own ADR.

## Consequences

- A call that the wrapper does not see (for example Streamlit, which builds its own client and emits no
  telemetry) is unobserved; the API and live evaluation are covered.
- Provider retries are folded into one event (`llm_retries`); a structured-output repair is a second call and
  therefore a second event, which makes repair rates visible per route.
- Estimated cost is only as good as the configured prices and the provider's reported counts.

## Invariants

- `LLMCallRecord` and `llm.call` carry no prompt, completion, SQL, row, excerpt, file name, or exception text.
- Observation failures never change the outcome of a model call.
- Every new telemetry attribute is added to `ALLOWED_ATTRIBUTES` with a declared shape.

## Addendum (2026-10-06): metrics

Issue #15 asks for vendor-neutral export. `MetricsRegistry`, a `TelemetrySink`, derives counters and
histograms from the same allowlisted events and serves them in Prometheus text format at `GET /metrics` when
`METRICS_ENABLED=true`. Its labels are
allowlisted tokens only, enumerated labels accept known values only, and each label is capped at 64 distinct
values, so it cannot become a side channel for content or identity. It is off by default, outside `/api`, and
not proxied by the bundled web server. SLIs, objectives, and operator queries are in
[slos.md](../operations/slos.md).

## Addendum (2026-10-06): opt-in OTLP traces

`TraceManager` owns a private OpenTelemetry `TracerProvider` and OTLP/gRPC `BatchSpanProcessor`; it never
installs a global provider. Trace export is absent unless `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set. The
endpoint must be a credential-free `http(s)` origin, and exporter shutdown uses a bounded flush. Manual
instrumentation is the privacy control: an API root gets only method, route *template*, status, safe error
code, and the opaque request ID; child spans come only from sanitized `TelemetryEvent` values. Exception
recording is disabled. An incoming W3C `traceparent` supplies the parent without accepting `tracestate` or
exporting request headers, and
a bounded map retains the root `SpanContext` for background job events.

The SDK and OTLP/gRPC exporter were already present in `requirements.lock` and the API image through
ChromaDB, so making them direct application dependencies adds no package or image-size delta. They remain
covered by the offline license policy and container Trivy/size gates. The optional collector/profile remains
deferred under #15.
