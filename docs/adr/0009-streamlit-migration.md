# ADR 0009: Streamlit migration and exit criteria

- Status: Accepted; disposition decided by [ADR 0011](0011-streamlit-disposition.md)
- Date: 2026-09-28
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6)

## Context

Streamlit is the working reference product. Removing it before React/FastAPI
reaches parity would discard a reliable comparison surface and force a risky
big-bang rewrite.

## Decision

Use a strangler migration. First extract framework-neutral services, then add a
FastAPI vertical slice and the corresponding React journey. Keep Streamlit
operational until the parity criteria below are demonstrated.

Streamlit may be removed or retained as a developer-only diagnostic UI only
after a follow-up ADR records the evidence and final choice.

## Exit criteria

- All supported upload, schema, analytics, anomaly, SQL, RAG, hybrid, report,
  export, consent, reset, retention, and deletion workflows have parity tests.
- Cross-tenant and safety regression suites pass through the API boundary.
- The React client passes accessibility and critical browser journeys.
- Docker Compose smoke, restart, backup/restore, and local-only checks pass.
- Migration and rollback instructions are documented.

## Consequences

- Some temporary duplication in composition and UI is intentional.
- New product features should prefer framework-neutral services so they are not
  implemented twice.
- Parity is evidence-based rather than a visual resemblance judgment.

## Invariants

- Streamlit remains the behavioral reference until the exit criteria are met.
- Its removal is not bundled into the first React or FastAPI pull request.

## Phase 8 disposition

[ADR 0011](0011-streamlit-disposition.md) applies these criteria to the recorded parity,
accessibility, browser, container, and migration evidence. React/FastAPI is now the primary local
product, while Streamlit remains a developer-only compatibility/reference surface until API OIDC,
workspace continuity, complete retention evidence, and the other retirement conditions are met.
