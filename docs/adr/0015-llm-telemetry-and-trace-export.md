# ADR 0015: Content-free LLM call telemetry; no trace export yet

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
6. **No trace exporter and no Langfuse/Phoenix profile in this change.** The deployment gap this would fill is
   the exporter delivered under issue #15; adding a second export path here would duplicate it. When #15 lands,
   spans must be built from `LLMCallRecord` only (purpose, provider, model, duration, counts, outcome), with
   any content-capture feature of the tracing tool disabled and that asserted by a test. A profile must stay
   out of default `compose.yaml`, loopback-only, digest-pinned, and non-root. Content-bearing tracing for
   local debugging would conflict with ADR 0007 and needs its own ADR.

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

## Addendum (2026-10-06): metrics, not traces

Issue #15 asks for vendor-neutral export. This ADR's deferral of trace export stands (spans need an SDK
dependency, an image-size and license review, and their own privacy tests). What was added instead needs no
dependency: `MetricsRegistry`, a `TelemetrySink` that derives counters and histograms from the same allowlisted
events and serves them in Prometheus text format at `GET /metrics` when `METRICS_ENABLED=true`. Its labels are
allowlisted tokens only, enumerated labels accept known values only, and each label is capped at 64 distinct
values, so it cannot become a side channel for content or identity. It is off by default, outside `/api`, and
not proxied by the bundled web server. SLIs, objectives, and operator queries are in
[slos.md](../operations/slos.md). Trace export remains open under #15.