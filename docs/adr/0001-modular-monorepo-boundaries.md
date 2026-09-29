# ADR 0001: Modular monorepo boundaries

- Status: Accepted
- Date: 2026-09-28
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6)

## Context

The current application is organized by responsibility under `src/`, but
Streamlit callbacks still own composition, browser-session state, and several
resource lifecycles. React, an HTTP API, and an optional worker need to reuse
the same behavior without importing a UI framework into the domain.

## Decision

Adopt the following target boundaries incrementally:

```text
apps/web
apps/api
apps/worker             # only if ADR 0005's measured trigger is met
packages/agents
packages/analytics
packages/retrieval
packages/connectors
packages/governance
packages/observability
```

Application entry points may depend on packages. Packages must not depend on
React, Streamlit, FastAPI request objects, or worker transport objects.
Framework-neutral commands and result types form the application boundary.

Existing `src/` modules move only when a child issue has characterization
coverage for the behavior being moved. The target directories are not created
as empty scaffolding in this iteration.

## Consequences

- Streamlit and FastAPI can coexist during migration.
- Provider and persistence implementations remain adapters rather than domain
  dependencies.
- The change requires temporary compatibility imports while modules move.
- A boundary check must eventually run in CI to prevent inward dependencies
  from importing application entry points.

## Invariants

- Deterministic enforcement remains independent of the LLM provider.
- No package accepts a client-supplied tenant identifier as authorization.
- Moving code must not change existing outputs without an explicit behavioral
  decision and regression update.
