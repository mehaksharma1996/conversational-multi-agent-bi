# Frontend bundle budget (issue #13)

## Problem

The dashboard's charts pulled in Plotly's default entry (`react-plotly.js` imports the full
`plotly.js`), producing one lazily loaded chunk of about 4.6 MB minified. It was already code-split
(the chunk loads only when an analysis with charts is shown), but every analyst paid for every trace
type, WebGL, maps, and 3D support that the product never uses.

## Baseline (before)

Measured on the production build (`npm run build`, Vite 8) with Node's zlib at gzip level 9 and the
default brotli quality:

| Asset | Raw | gzip | brotli |
|---|---:|---:|---:|
| Plotly chunk (`dist-*.js`) | 4,566 KiB | 1,373 KiB | 1,022 KiB |
| Application entry (`index-*.js`) | 258 KiB | 78 KiB | 68 KiB |
| CSS | 10 KiB | 3 KiB | 3 KiB |

## What the product actually draws

`src/charts/chart_builder.py` builds every figure with Plotly Express `line`, `bar`, `histogram`, and
`scatter`: only cartesian traces. `tests/test_chart_trace_types.py` fails if the server starts using
any other constructor.

## Options considered

| Option | Published size (unpacked) | Verdict |
|---|---:|---|
| Keep the default entry (full `plotly.js`) | about 6.1 MB | Baseline; ships unused trace types |
| Further code-splitting or lazy loading | no change to the chunk itself | Already lazy; splitting one library further gains nothing |
| `plotly.js-basic-dist-min` | 1.2 MB | Smaller, but has no `histogram` trace, which the product uses |
| **`plotly.js-cartesian-dist-min`** | **1.5 MB** | **Chosen:** covers scatter/line, bar, and histogram |
| `plotly.js-strict-dist-min` | 5.2 MB | Needs no `eval`-style code, but is barely smaller than the full bundle |
| A different chart library | unknown | Rejected: rewrites every figure and the accessibility baseline for a gain the partial bundle already delivers |

CSP is unchanged: the partial bundle is the same `*-dist-min` build family as the one the CSP
tests already run, and the real-stack journey in CI still asserts that nginx reports no CSP
violations. Accessibility is unchanged: the dashboard keeps its titles, descriptions, and tables, and
the axe baseline "dashboard with charts" still passes (the Plotly subtree remains excluded from axe,
as recorded in `accessibility-baseline.md`).

## Result (after)

| Asset | Raw | gzip | brotli | Change |
|---|---:|---:|---:|---|
| Plotly chunk (`PlotlyChart-*.js`) | 1,409 KiB | 460 KiB | 370 KiB | −69% raw, −67% gzip, −64% brotli |
| Application entry | 258 KiB | 78 KiB | 68 KiB | unchanged |

`plotly.js-dist-min` was also listed as a dependency but never imported; it has been removed.

## How it is implemented

`src/components/PlotlyChart.tsx` calls `react-plotly.js/factory` with
`plotly.js-cartesian-dist-min`, and `AnalysisDashboard` lazy-loads that component. Types for the
partial bundle reuse `@types/plotly.js` (`src/types/plotly-cartesian.d.ts`).

## Budget and regression detection

`apps/web/bundle-budget.json` holds the limits; `npm run check:bundle` (run by the CI `frontend` job
after `npm run build`) fails when any is exceeded:

| Measure | Budget | Measured |
|---|---:|---:|
| Entry chunk, gzip | 100 KiB | 78 KiB |
| Largest (chart) chunk, raw | 1,600 KiB | 1,409 KiB |
| Largest (chart) chunk, gzip | 520 KiB | 460 KiB |
| All JavaScript, gzip | 620 KiB | 538 KiB |

Budgets leave about 10 to 15 percent of headroom so an ordinary dependency update does not fail the
build, while adding a heavy dependency or reverting to the full Plotly entry does. Raising a budget
needs a justification in this file. The checker was exercised with a deliberately tight budget to confirm
that it fails.

## What was not measured

Load and interaction timings on real devices were not measured; this record covers transfer size and
bundle composition, which are deterministic and enforced in CI. A first-chart-render timing in the
Playwright journey would be a reasonable follow-up, but shared CI runners are too noisy to gate on it
(see `docs/operations/benchmarks.md`).

## Limits and follow-ups

- A new server-side chart type outside the cartesian bundle must be accompanied by switching the web
  bundle; the guard test makes that an explicit decision rather than a blank chart.
- Plotly's own `plotly.js` is still installed as a peer dependency of `react-plotly.js`; it is not bundled.
