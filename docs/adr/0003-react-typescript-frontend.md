# ADR 0003: React and TypeScript frontend

- Status: Accepted
- Date: 2026-09-28
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6)

## Context

Streamlit provides the complete current product experience, but its server-side
rerun model couples presentation and application state. The modernization needs
a portfolio-quality browser client without duplicating backend rules.

## Decision

Build `apps/web` with React and strict TypeScript. Use Vite for the application
build, Vitest and React Testing Library for component behavior, and Playwright
for critical browser journeys. Use an OpenAPI-generated client behind a small
feature-oriented data-access layer; components do not issue ad hoc HTTP calls.

Server state remains authoritative. Client state is limited to presentation,
in-progress edits, and bounded conversation display. Plotly remains the chart
renderer so the new UI preserves the current visualization semantics.

The first vertical slice is tabular upload through schema review and
deterministic analysis. PDF/RAG, conversational SQL, reports, and exports follow
only after the pattern is proven.

## Consequences

- The repository gains a pinned Node toolchain and frontend quality gates.
- API design must support explicit loading, retry, progress, and cancellation
  states that Streamlit previously managed through reruns.
- Accessibility and responsive behavior become tested product requirements.

## Invariants

- The frontend never reimplements SQL safety, authorization, retention, or
  model-governance decisions.
- Gemini consent, local-only behavior, warnings, citations, generated SQL, and
  decision-support disclaimers remain visible at feature parity.
