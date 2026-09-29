# ADR 0007: Observability and privacy

- Status: Accepted
- Date: 2026-09-28
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6)

## Context

The current code emits useful privacy-safe log lines for routes, provider usage,
retrieval, timeouts, storage, and cleanup. It does not yet propagate one context
through browser, API, jobs, providers, and stores or expose metrics and traces.

## Decision

Use vendor-neutral observability interfaces and OpenTelemetry-compatible
instrumentation. Propagate a request ID and optional job ID through every layer.
Structured logs, metrics, traces, and audit records share stable event names but
remain separate concerns.

Raw questions, document excerpts, uploaded values, SQL literals, credentials,
tokens, and unredacted identity data are forbidden from default telemetry.
Debug content capture remains explicit, local-only, short-lived, and visibly
unsafe. The local Docker topology may provide an optional telemetry profile;
no hosted vendor is selected here.

## Consequences

- Instrumentation can be tested without a production backend.
- Cardinality and sensitive-field reviews become part of telemetry changes.
- Service-level indicators can be derived consistently across execution modes.

## Implementation status (Phase 6)

Implemented for the FastAPI service: request-ID propagation, allowlist-only structured telemetry
(`packages/observability/`), separate append-only audit events (`packages/governance/`), and safe error categories.
Not implemented: metrics, tracing, an OpenTelemetry exporter, job IDs, and a local telemetry profile.
See [evaluation, governance, and observability](../architecture/evaluation-governance-observability.md).

## Invariants

- User-visible errors contain a request ID, not internal exception detail.
- Tenant identifiers in telemetry are opaque or hashed.
- Disabling exporters does not disable application audit requirements.
