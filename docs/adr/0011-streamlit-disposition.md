# ADR 0011: Retain Streamlit as a developer-only compatibility surface

- Status: Accepted
- Date: 2026-09-29
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6) (Phase 8)

## Context and evidence

ADR 0009 requires workflow parity, safety/isolation evidence, accessible critical browser journeys,
container operations evidence, and migration/rollback instructions before deciding Streamlit's
status. The row-by-row evidence is in
[streamlit-parity-evidence.md](../architecture/streamlit-parity-evidence.md), the accessibility scope
is in [accessibility-baseline.md](../architecture/accessibility-baseline.md), and operator transition
steps are in [migration-and-rollback.md](../operations/migration-and-rollback.md).

The core analyst journey is proven through the API and React client, including real-stack tabular,
memory, PDF/RAG, hybrid, report, export, and reset browser tests. Cross-tenant, SQL, retrieval,
prompt-injection, audit, telemetry, and evaluation suites remain in place. The Chroma replacement
path now serializes promotion and refreshes live readers after a canonical collection ID changes.

Three facts prevent removal:

1. Streamlit has optional OIDC; the API has only a fixed local-development identity and must remain
   loopback-only. API OIDC is tracked by [#9](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9).
2. API resource metadata is process-local. ADR 0010 waives restart persistence; durable metadata,
   migrations, backup/restore, and recovery are [#12](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/12).
3. Retention behavior exists but governance/configuration and verified cross-store cleanup remain
   partial under [#18](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/18).

## Decision

React/FastAPI is the primary local product. Retain `app.py` and `src/ui/` as a developer-only
compatibility and behavioral-reference surface, available through the explicit `streamlit` Compose
profile and direct local development command. Do not advertise it as the production-shaped UI and
do not add product features only to Streamlit. Changes are limited to shared-core adoption,
security/reliability fixes, and work needed to compare or retire the surface.

Streamlit is not removed in Phase 8. Keeping it is not an authentication substitute for the API and
does not authorize non-loopback exposure.

## Retirement conditions

Removal requires a new ADR showing all of the following:

- provider-neutral API OIDC/authentication and authorization are shipped and negative-tested;
- workspace continuity is durable, or a replacement migration model and waiver is explicitly
  accepted for the intended users;
- every row in the parity evidence is proven, including retention/deletion governance;
- the real-stack, accessibility, isolation, evaluation, OpenAPI compatibility, Compose restart,
  and rollback checks pass at the removal commit; and
- operations/governance documentation has been independently exercised ([#11](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/11)).

## Rollback and consequences

During a React/API regression, run the Streamlit profile on loopback and use it to compare behavior.
No active workspace moves between surfaces. Upgrade rollback uses the previous tag/commit, rebuilds
Compose, verifies the audit chain, and requires source re-upload; see the migration runbook.

This decision leaves temporary UI composition code and Streamlit dependencies in the repository.
It avoids claiming parity where identity, durability, and retention evidence are incomplete, while
making ownership clear: new product work belongs in the framework-neutral core and React/API path.
