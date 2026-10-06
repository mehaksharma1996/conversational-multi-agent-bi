# Architecture Decision Records

These records define the guardrails for the modernization tracked by
[GitHub issue #6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6).
They record migration invariants and the evidence-based decisions made as the
React/FastAPI runtime became the primary local product.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-modular-monorepo-boundaries.md) | Modular monorepo boundaries | Accepted |
| [0002](0002-versioned-api-contract.md) | Versioned API and OpenAPI contract | Accepted |
| [0003](0003-react-typescript-frontend.md) | React and TypeScript frontend | Accepted |
| [0004](0004-identity-session-tenancy.md) | Identity, sessions, and tenancy | Accepted |
| [0005](0005-job-execution-boundary.md) | Job execution boundary | Accepted, implementation deferred |
| [0006](0006-persistence-concurrency-lifecycle.md) | Persistence and lifecycle | Accepted |
| [0007](0007-observability-privacy.md) | Observability and privacy | Accepted |
| [0008](0008-ai-evaluation-policy.md) | AI evaluation policy | Accepted |
| [0009](0009-streamlit-migration.md) | Streamlit migration and exit criteria | Accepted; disposition decided by 0011 |
| [0010](0010-local-container-release.md) | Local container release and restart-persistence waiver | Accepted |
| [0011](0011-streamlit-disposition.md) | Retain Streamlit as a developer-only compatibility surface | Accepted |
| [0012](0012-deliberate-non-goals.md) | Deliberate non-goals for the local BI product | Accepted |
| [0013](0013-browser-authentication-transport.md) | Browser authentication transport (BFF session, CSRF, no CORS) | Accepted |
| [0014](0014-mcp-server.md) | Read-only MCP server over the guarded data core (stdio) | Accepted |

## ADR lifecycle

- `Proposed`: under review and not yet a dependency for implementation.
- `Accepted`: the default for subsequent child issues and pull requests.
- `Superseded`: replaced by another ADR; the replacement must be linked.
- `Deprecated`: retained for history but no longer applicable.

Changing an accepted decision requires a new ADR. Existing records are not
rewritten to hide the earlier trade-off.
