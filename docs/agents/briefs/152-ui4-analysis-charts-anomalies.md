# UI-4: analysis, visualizations, and anomalies (#152)

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/152
Tier: medium. Check mode: `--full` (API model and web changes).

**Status (2026-11-16):** S1 and S2 implemented locally with scripted and mocked evidence only.
Implementation note: lazy-loading the dashboard made the post-run focus move race the chunk load
(the heading did not exist yet). `App` therefore records a focus request with the user's intent at
completion, and the dashboard schedules the move once mounted and reports it handled, so a later
remount never replays it.

## Goal

The analysis stage of the workbench meets the same bar as UI-2 and UI-3: every chart has an
accessible alternative that states the same numbers, anomaly and classifier results read as review
candidates with clear counts, and running, failed, and replaced analyses follow the UI-2 operation
model.

## Baseline (verified against `3f53d70` on 2026-11-16)

- `apps/web/src/components/AnalysisDashboard.tsx` renders the server's Plotly figures with no text
  or tabular alternative, a hard-coded light chart font (`#cbd8ea`), and is imported eagerly by
  `App.tsx`. The entry chunk is close to its 105 KiB gzip budget (`bundle-budget.json`).
- `src/charts/chart_builder.py` builds line (trend, precision-recall), histogram, bar, and scatter
  (anomaly) charts with `plotly.express`. `figure.to_json()` (Plotly 6) encodes arrays as base64
  typed arrays, and the histogram carries raw values with Plotly-computed bins. A table derived in
  the browser would need binary decoding and could disagree with the bars.
- `ChartResponse` (`apps/api/models.py`) has `title`, `chart_type`, `description`, `metadata`, and
  `figure`. `handleAnalysis` already runs in the UI-2 mutation lane.

## Implementation

**S1: accessible alternatives from the server (additive API change).**
1. `ChartSpec` gains a `ChartAlternative`: a deterministic one-sentence `summary`, labelled
   `columns`, `rows` (at most 50, in figure order), `total_rows`, and `truncated`. It is built from
   the same frame as the figure, never from the rendered figure.
2. The histogram uses explicit bins (`numpy.histogram_bin_edges`, 20 bins), set on the figure as
   `xbins`, so the table's bin counts equal the bars exactly. No other chart changes.
3. `ChartResponse.alternative` exposes it, with an optional default so older clients are unaffected.
   Regenerate the OpenAPI document and the web types. The compatibility check must pass.

**S2: the web workbench.**
1. Lazy-load the analysis dashboard to keep the entry chunk within budget.
2. Each chart is a `figure` with its title as the caption, the summary as its description, and a
   "Show the data as a table" disclosure that renders the alternative, labelled when truncated.
   Plotly's SVG is hidden from assistive technology, so the summary and table are the alternative.
3. Chart text colours come from the theme tokens, so light and dark themes are both legible.
4. Anomaly and classifier sections: an explicit empty state, the shortlist counts stated in words,
   and the existing review-candidate wording kept.
5. The analysis run keeps the UI-2 lane. A failure shows the error with a retry, and replacing the
   dataset clears a stale analysis. Focus moves to the dashboard heading after a successful run.

## Tests and evidence

- pytest: an alternative for every chart type; histogram table counts equal the figure's bins;
  truncation at 50 rows; the summaries are deterministic; the API response carries the alternative.
- OpenAPI regeneration and `python -m scripts.check_openapi_compatibility --base-ref origin/main`.
- Vitest: figure semantics, the disclosure, truncation, the empty states, theme colours, focus after
  a run, and failure and retry.
- Playwright (mocked): the analysis journey with axe, keyboard use of the disclosure, and a narrow
  viewport. `npm run check:bundle`.
- `python -m scripts.check_all --full`.

## Security and non-goals

- No provider, consent, budget, evaluation, telemetry or audit, tenant, or retention change. The
  alternative carries values the figure already sends to the same user; nothing new is logged or
  stored in the browser.
- No new chart types, chart interactivity, or analysis features. No change to anomaly or classifier
  methods or thresholds.
