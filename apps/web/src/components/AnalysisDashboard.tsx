import { lazy, Suspense, useEffect, useId, useRef } from "react";
import type { Config, Data, Layout } from "plotly.js";

import type { Analysis } from "../api/client";

type Chart = Analysis["charts"][number];
type ChartAlternative = NonNullable<Chart["alternative"]>;
import { focusOnNextFrame, type FocusIntent } from "../workspace/focus";
import { StatusBanner } from "./StatusBanner";

const Plot = lazy(() => import("./PlotlyChart"));

interface AnalysisDashboardProps {
  analysis: Analysis;
  downloadBusy: boolean;
  onDownloadReport: (format: "markdown" | "pdf") => Promise<void>;
  /** A finished run's focus request; the move happens once this lazily loaded view has mounted. */
  focusRequest?: FocusIntent | null;
  /** Called once the request is scheduled, so a later remount never replays a stale move. */
  onFocusRequestHandled?: () => void;
}

interface PlotlyFigure {
  data?: Data[];
  layout?: Partial<Layout>;
  config?: Partial<Config>;
}

/** Chart text uses the theme's secondary text token rather than a fixed colour (UI-4, #152). */
function chartTextColour(): string {
  if (typeof window === "undefined") return "#b5c0cf";
  const token = window.getComputedStyle(document.documentElement).getPropertyValue("--text-secondary").trim();
  return token || "#b5c0cf";
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  switch (typeof value) {
    case "number":
      return value.toLocaleString(undefined, { maximumFractionDigits: 3 });
    case "string":
      return value;
    case "boolean":
      return value ? "true" : "false";
    case "bigint":
      return value.toString();
    case "object":
      return JSON.stringify(value);
    default:
      return "—";
  }
}

function RecordsTable({ caption, records }: { caption: string; records: Record<string, unknown>[] }) {
  const columns = Array.from(new Set(records.flatMap((record) => Object.keys(record))));
  if (records.length === 0 || columns.length === 0) return null;

  return (
    <div className="table-scroll" role="region" tabIndex={0} aria-label={caption}>
      <table>
        <caption>{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th scope="col" key={column}>{column.replaceAll("_", " ")}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {records.map((record, rowIndex) => (
            <tr key={`${caption}-${rowIndex}`}>
              {columns.map((column) => <td key={column}>{displayValue(record[column])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AlternativeTable({ title, alternative }: { title: string; alternative: ChartAlternative }) {
  const shown = alternative.rows.length;
  return (
    <details className="chart-data">
      <summary>Show the data as a table</summary>
      <div className="table-scroll" role="region" tabIndex={0} aria-label={`${title} data`}>
        <table>
          <caption>
            {title}
            {alternative.truncated
              ? ` (first ${shown.toLocaleString()} of ${alternative.total_rows.toLocaleString()} rows)`
              : ""}
          </caption>
          <thead>
            <tr>
              {alternative.columns.map((column, index) => (
                <th scope="col" key={`${index}-${column}`}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {alternative.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => <td key={cellIndex}>{displayValue(cell)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function ChartCard({ chart }: { chart: Chart }) {
  const id = useId();
  const figure = chart.figure as PlotlyFigure;
  const alternative = chart.alternative ?? null;
  return (
    <figure
      className="chart-card"
      aria-labelledby={`${id}-title`}
      aria-describedby={alternative ? `${id}-summary` : undefined}
    >
      <figcaption>
        <h3 id={`${id}-title`}>{chart.title}</h3>
        <p>{chart.description}</p>
        {alternative ? (
          <p className="chart-summary" id={`${id}-summary`}>{alternative.summary}</p>
        ) : (
          <p className="chart-summary">No text alternative is available for this chart.</p>
        )}
      </figcaption>
      {/* The drawing repeats the summary and table below for sighted readers; assistive technology
          gets the text instead of Plotly's unlabelled SVG. */}
      <div aria-hidden="true">
        <Suspense fallback={<p className="chart-loading">Loading chart…</p>}>
          <Plot
            data={figure.data ?? []}
            layout={{
              ...figure.layout,
              autosize: true,
              paper_bgcolor: "transparent",
              plot_bgcolor: "transparent",
              font: { ...(figure.layout?.font ?? {}), color: chartTextColour() },
            }}
            config={{ ...figure.config, displaylogo: false, responsive: true, displayModeBar: false }}
            useResizeHandler
            style={{ height: "360px", width: "100%" }}
          />
        </Suspense>
      </div>
      {alternative ? <AlternativeTable title={chart.title} alternative={alternative} /> : null}
    </figure>
  );
}

export function AnalysisDashboard({
  analysis,
  downloadBusy,
  onDownloadReport,
  focusRequest = null,
  onFocusRequestHandled,
}: AnalysisDashboardProps) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    // The user's intent at completion still decides: typing or pointer use since then cancels it.
    if (!focusRequest) return;
    focusOnNextFrame(() => heading.current, focusRequest);
    onFocusRequestHandled?.();
  }, [focusRequest, onFocusRequestHandled]);
  // The listed rows are a ranked shortlist capped by the API; the total counts every flagged row.
  const shortlist = `${analysis.anomaly.flagged_count.toLocaleString()} of ${analysis.anomaly.total_flagged_count.toLocaleString()}`;
  return (
    <section className="panel analysis-panel" aria-labelledby="analysis-results-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Deterministic results</p>
          <h2 id="analysis-results-heading" ref={heading} tabIndex={-1}>Analysis dashboard</h2>
        </div>
      </div>

      <StatusBanner kind="info">
        Anomaly flags and classifier scores are review candidates, not confirmed fraud,
        misconduct, or eligibility. This workbench supports human decisions; it does not make
        autonomous decisions.
      </StatusBanner>

      <div className="button-row report-actions" aria-label="Report downloads">
        <button className="button button--secondary" type="button" disabled={downloadBusy} onClick={() => void onDownloadReport("markdown")}>Download Markdown report</button>
        <button className="button button--secondary" type="button" disabled={downloadBusy} onClick={() => void onDownloadReport("pdf")}>Download PDF report</button>
      </div>

      <div className="metrics-grid" aria-label="Analysis summary">
        {Object.entries(analysis.dataset_summary).map(([label, value]) => (
          <div className="metric-card" key={label}>
            <span>{label.replaceAll("_", " ")}</span>
            <strong>{value.toLocaleString()}</strong>
          </div>
        ))}
        <div className="metric-card metric-card--accent">
          <span>Anomalies flagged</span>
          <strong>{analysis.anomaly.total_flagged_count.toLocaleString()}</strong>
          {analysis.anomaly.flagged_count > 0 ? (
            <small className="fine-print">Top {shortlist} listed for review</small>
          ) : null}
        </div>
        {analysis.classification.metrics ? (
          <div className="metric-card metric-card--accent">
            <span>Classifier PR-AUC</span>
            <strong>{analysis.classification.metrics.pr_auc.toFixed(3)}</strong>
          </div>
        ) : null}
      </div>

      <div className="capability-list" aria-label="Analysis capabilities">
        {analysis.capabilities.map((capability) => (
          <article
            className={`capability ${capability.available ? "is-ready" : "is-unavailable"}`}
            key={capability.name}
          >
            <span aria-hidden="true">{capability.available ? "✓" : "–"}</span>
            <div>
              <h3>{capability.name.replaceAll("_", " ")}</h3>
              <p>{capability.reason}</p>
            </div>
          </article>
        ))}
      </div>

      {analysis.analytics_limitations.map((limitation) => (
        <StatusBanner kind="info" key={limitation}>{limitation}</StatusBanner>
      ))}

      {analysis.charts.length > 0 ? (
        <div className="chart-grid">
          {analysis.charts.map((chart) => (
            <ChartCard chart={chart} key={`${chart.title}-${chart.chart_type}`} />
          ))}
        </div>
      ) : (
        <p className="fine-print">No charts apply to this dataset's confirmed schema.</p>
      )}

      <div className="results-grid">
        <article className="result-card">
          <h3>Anomaly review</h3>
          <dl className="definition-grid">
            <div><dt>Method</dt><dd>{analysis.anomaly.method}</dd></div>
            <div><dt>Unique rows flagged</dt><dd>{analysis.anomaly.total_flagged_count}</dd></div>
            <div><dt>Model flagged</dt><dd>{analysis.anomaly.model_flagged_count}</dd></div>
            <div><dt>Rule flagged</dt><dd>{analysis.anomaly.rule_flagged_count}</dd></div>
            <div><dt>Features</dt><dd>{analysis.anomaly.feature_columns.join(", ") || "None"}</dd></div>
          </dl>
          {analysis.anomaly.limitations.map((limitation) => (
            <p className="fine-print" key={limitation}>{limitation}</p>
          ))}
          <p className="fine-print">
            A row can be flagged by both the model and the rules; it is counted once.
          </p>
          {analysis.anomaly.total_flagged_count === 0 ? (
            <p>No rows were flagged for review with the selected features and contamination.</p>
          ) : (
            <RecordsTable
              caption={`Review shortlist: top ${shortlist} flagged rows by anomaly score`}
              records={analysis.anomaly.flagged_rows}
            />
          )}
        </article>

        <article className="result-card">
          <h3>Supervised classification</h3>
          {analysis.classification.enabled && analysis.classification.metrics ? (
            <>
              <dl className="definition-grid">
                <div><dt>Model</dt><dd>{analysis.classification.method.replaceAll("_", " ")}</dd></div>
                <div><dt>Label</dt><dd>{analysis.classification.label_column ?? "Unknown"}</dd></div>
                <div><dt>Precision</dt><dd>{analysis.classification.metrics.precision.toFixed(3)}</dd></div>
                <div><dt>Recall</dt><dd>{analysis.classification.metrics.recall.toFixed(3)}</dd></div>
                <div><dt>F1</dt><dd>{analysis.classification.metrics.f1.toFixed(3)}</dd></div>
                <div><dt>ROC-AUC</dt><dd>{analysis.classification.metrics.roc_auc.toFixed(3)}</dd></div>
                <div><dt>PR-AUC</dt><dd>{analysis.classification.metrics.pr_auc.toFixed(3)}</dd></div>
                <div><dt>Threshold</dt><dd>{analysis.classification.metrics.threshold.toFixed(3)}</dd></div>
              </dl>
              <p className="fine-print">
                These scores rank cases for human review only. They do not prove an outcome and
                must not trigger automated decisions.
              </p>
              {analysis.classification.limitations.map((limitation) => (
                <p className="fine-print" key={limitation}>{limitation}</p>
              ))}
              <RecordsTable
                caption="Classifier candidates for human review"
                records={analysis.classification.review_candidates}
              />
            </>
          ) : (
            <p>{analysis.classification.reason}</p>
          )}
        </article>

        <article className="result-card report-card">
          <p className="eyebrow">Generated from deterministic outputs</p>
          <h3>{analysis.report.title}</h3>
          {analysis.report.sections.map((section) => (
            <section key={section.title}>
              <h4>{section.title}</h4>
              {section.body ? <p>{section.body}</p> : null}
              {section.bullets.length > 0 ? (
                <ul>{section.bullets.map((bullet) => <li key={bullet}>{bullet}</li>)}</ul>
              ) : null}
            </section>
          ))}
        </article>
      </div>

      <details className="profile-table-wrap">
        <summary>Inspect numeric summary</summary>
        <RecordsTable caption="Numeric summary" records={analysis.numeric_summary} />
      </details>
    </section>
  );
}
