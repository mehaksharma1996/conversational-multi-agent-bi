# ADR 0002: Versioned API and OpenAPI contract

- Status: Accepted
- Date: 2026-09-28
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6)

## Context

The current UI calls Python functions directly. A separately built web client
needs a stable, typed contract for uploads, analysis, conversations, jobs,
reports, and workspace lifecycle operations.

## Decision

Expose product operations through FastAPI under `/api/v1`. Pydantic models are
the source for request and response schemas. A reproducible
`openapi/openapi.json` is committed and checked for unreviewed breaking changes.
The React client uses generated TypeScript types and operations from that
artifact rather than hand-maintained duplicate interfaces.

Errors use a stable envelope containing a machine-readable code, safe message,
request ID, and optional field details. Internal exceptions and sensitive
payloads are never returned. Large downloads are streamed. Long-running work
returns `202 Accepted` with a tenant-owned job resource when ADR 0005 applies.

The initial resource and endpoint model is documented in
`docs/architecture/api-resource-model.md`.

## Consequences

- API and frontend changes become independently testable.
- OpenAPI compatibility becomes a merge gate.
- Contract evolution requires additive changes within v1 or an explicitly
  planned new version.
- Session-state objects and pandas dataframes cannot cross the HTTP boundary
  without explicit serialization models.

## Invariants

- The API preserves existing upload limits, SQL controls, consent, redaction,
  and safe-export behavior.
- Tenant ownership is enforced on every resource lookup.
- Health endpoints do not disclose credentials or sensitive configuration.
