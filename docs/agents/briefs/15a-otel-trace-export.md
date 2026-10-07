# Brief: #15 privacy-safe OTLP trace export

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/15
Tier: strongest. Check mode: --full. Decision records: [ADR 0007](../../adr/0007-observability-privacy.md)
and [ADR 0015](../../adr/0015-llm-telemetry-and-trace-export.md).

## Outcome

The FastAPI service can opt in to OTLP/gRPC trace export without changing its default behavior. One
server span accepts W3C trace context, and the already-sanitized telemetry stream becomes child spans
covering request, orchestration, provider, storage, audit, and background-job events; no content-bearing
field or exception detail can enter a span.

## Facts (verified against the code on 2026-10-06)

- `packages/observability/telemetry.py:Telemetry.emit` is the deny-by-default boundary: event names are
  validated and attributes are allowlisted and shape-checked before a `TelemetrySink` sees them.
- `apps/api/main.py:create_app` already fans sanitized events out to logging and optional metrics and owns
  the request-ID middleware and application shutdown lifecycle.
- `packages/jobs/executor.py:InProcessJobExecutor` preserves the originating `request_id` on every
  `JobEvent`, including events emitted from worker threads.
- `packages/governance/audit.py:AuditRecorder.record` emits telemetry only when a write fails, so successful
  audit persistence has no trace boundary today.
- The resolved lock already contains OpenTelemetry 1.45 and the OTLP/gRPC exporter transitively, but
  `requirements.txt` does not declare the SDK/exporter as direct application dependencies.

## Do

1. Add a framework-neutral tracing module under `packages/observability/` that owns a private
   `TracerProvider`, OTLP/gRPC batch exporter, W3C trace-context extraction, bounded request-context
   correlation for job threads, and a `TelemetrySink` that consumes only `TelemetryEvent`.
2. Add one optional, validated OTLP traces endpoint setting. Keep tracing off when it is absent; never
   accept exporter headers or credentials through telemetry configuration.
3. Compose the tracing sink and request-root span in `apps/api/main.py`; flush/shut down the provider in
   the lifespan. Keep health and metrics outside request tracing, as they are outside request metrics.
4. Emit one sanitized `audit.write` telemetry event for successful and failed audit writes while retaining
   the existing `audit.write_failed` compatibility event.
5. Declare the existing pinned OpenTelemetry SDK and OTLP/gRPC exporter as direct dependencies. Run the
   offline license policy and record that the container dependency set and size do not grow because these
   packages were already transitively installed.
6. Add focused tests for disablement, parent/child and job correlation, flush behavior, failure isolation,
   W3C parent acceptance, and adversarial question/SQL/row/document/prompt/secret values never appearing in
   exported span names, attributes, events, status text, or resource attributes.
7. Update `.env.example`, ADRs 0007/0015, `docs/operations/slos.md`, and the incident runbook with the exact
   opt-in and privacy contract.

## Do not touch

- No Compose collector/scraper profile, alert rules, queue/storage/resource gauges, or retrieval changes;
  those are independently reviewable #15 follow-ups.
- No automatic FastAPI/SQL/HTTP-client instrumentation: it can capture URLs, query strings, statements,
  headers, exception messages, or other values outside the telemetry allowlist.
- No prompt, response, SQL, result row, document text, filename, raw tenant ID, secret, exception message,
  or stack trace in spans. Do not call OpenTelemetry exception-recording helpers.
- No public REST/OpenAPI change and no `evals/v1/` or prompt changes.
- Do not install a global tracer provider; tests and multiple in-process app instances must remain isolated.

## Acceptance (this independently mergeable split)

- [ ] With no endpoint configured, no SDK/exporter is constructed and current logging/metrics behavior is
  unchanged.
- [ ] With an endpoint configured, W3C-correlated API roots and sanitized event spans are exported through
  OTLP/gRPC; background job spans retain the originating request trace when the root has ended.
- [ ] Trace exporter failures never change API, job, or audit outcomes; shutdown attempts a bounded flush.
- [ ] Tests prove questions, SQL, rows, document text, prompts, filenames, identities, and secrets cannot be
  exported, including when supplied under allowlisted names.
- [ ] The OpenAPI snapshot is unchanged; license policy passes; `python -m scripts.check_all --full` passes.

## Evaluation and docs impact

No evaluation fixture, prompt registry, or baseline changes. Update the two existing observability ADRs and
operator docs; no new ADR is needed because the implementation follows ADR 0007 and closes ADR 0015's
explicit trace-export deferral.

## Design decisions already made

- Use OTLP/gRPC with `BatchSpanProcessor`, a fixed service name, and the versions already in
  `requirements.lock`; do not add auto-instrumentation or a vendor SDK.
- The `TelemetryEvent` allowlist is the only source of event-span attributes. Request roots receive only the
  validated HTTP method/route/status plus the opaque request ID.
- Use a bounded in-memory map from request ID to `SpanContext` solely to parent background-job event spans;
  neither key nor trace identifiers become metric labels.
- Treat an `http://` endpoint as explicit insecure transport and `https://` as TLS. Reject credentials,
  queries, fragments, and non-HTTP schemes at startup.
