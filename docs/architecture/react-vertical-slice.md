# React tabular vertical slice

Phase 4 introduces the first browser experience outside Streamlit. It proves
the React/FastAPI boundary using the complete tabular path rather than a static
shell: create a local workspace, upload CSV or Excel, select an Excel sheet,
review the canonical schema, configure anomaly detection, and inspect
deterministic analytics, Plotly figures, anomaly candidates, and the business
report structure.

## Boundaries

- `apps/web/src/api/schema.d.ts` is generated from `openapi/openapi.json`.
- `apps/web/src/api/client.ts` is the only HTTP data-access boundary used by
  the feature. Components do not construct API requests.
- FastAPI remains authoritative for upload limits, normalization, profiling,
  mapping confirmation, capability decisions, analytics, and anomalies.
- Browser state is limited to the active workflow, schema edits, feature
  selection, and results returned by the API.
- Vite proxies `/api` and `/health` to `127.0.0.1:8000` in development. This
  same-origin local setup does not define a hosting platform or production
  cross-origin policy.

The existing Streamlit application remains the behavioral reference and is
not removed by this phase.

## User journey

1. The client creates an idempotent, process-local workspace.
2. The analyst uploads a bounded CSV or Excel file. Excel workbooks require an
   explicit sheet choice before profiling.
3. The profile and canonical mapping suggestions are displayed for review.
   Suggestions below 50% confidence remain unselected.
4. The analyst confirms the mapping; this produces a versioned, ready dataset.
5. The analyst selects numeric anomaly features and an expected review
   proportion, then requests deterministic analysis.
6. The UI displays capability status, summaries, Plotly charts, separately
   counted model/rule anomaly flags, and a structured business report.

Anomaly results are visibly described as review candidates rather than
confirmed fraud or autonomous decisions.

## Local development

Start FastAPI from the repository root:

```powershell
& ".\.venv\Scripts\python.exe" -m uvicorn apps.api.main:app --reload
```

Start the web client in a second terminal:

```powershell
Set-Location apps/web
npm ci
npm run dev
```

The application is available at `http://127.0.0.1:5173`; interactive API docs
remain at `http://127.0.0.1:8000/docs`.

## Contract and quality gates

```powershell
Set-Location apps/web
npm run generate:api
npm run lint
npm run typecheck
npm run test
npm run build
npx playwright install chromium
npm run test:e2e
```

CI regenerates the typed schema and fails if it differs from the committed
file. Vitest and React Testing Library cover the workflow at the component
boundary; Playwright covers the same critical journey in Chromium with
contract-shaped API responses.

## Current limitations

This document records the Phase 4 boundary. Phase 5 subsequently migrated the
PDF, conversation, report, export, consent, reset, and retention workflows; see
[React and FastAPI feature parity](feature-parity.md).

- The FastAPI workspace repository remains process-local. Restarting the API
  removes resources created through this surface.
- Authentication remains local-development identity only at this API boundary.
- Long-running job progress and cancellation are not yet required by the
  synchronous tabular and document paths.
- The web build renders the full Plotly distribution when a chart is needed;
  bundle optimization is a later performance task once migrated chart types
  are fixed.
