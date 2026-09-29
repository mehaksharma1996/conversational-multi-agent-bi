# ADR 0004: Identity, sessions, and tenancy

- Status: Accepted
- Date: 2026-09-28
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6)

## Context

The current application optionally uses Streamlit OIDC and derives a stable,
path-safe tenant ID from the trusted `sub` claim. Each browser tab then receives
an isolated session workspace. FastAPI must preserve this boundary without
trusting tenant or session ownership asserted by the browser.

## Decision

Keep identity provider integration OIDC-compatible and provider-neutral. The API
derives tenant context from a verified identity claim. Resource identifiers may
be supplied for lookup, but authorization always compares their stored owner to
the server-derived tenant context.

Maintain an explicit local-development mode with a fixed local tenant and a
visible warning. Production-mode configuration fails closed when identity
settings are present but invalid. Authentication transport, CSRF protection,
CORS, and cookie/token details must be finalized in the FastAPI foundation child
issue before browser authentication is implemented.

## Consequences

- Current workspace paths can remain tenant/session scoped.
- Authorization becomes a reusable API dependency plus a repository-level
  ownership check.
- Negative cross-tenant tests are required for reads, queries, exports, jobs,
  reset, cancellation, and deletion.

## Invariants

- A raw tenant ID from a request is never authority.
- Logs, metrics, and traces use opaque or hashed identity values.
- Local-development bypass cannot activate silently when production identity
  configuration is malformed.
