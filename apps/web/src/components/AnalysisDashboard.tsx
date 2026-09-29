import { lazy, Suspense } from "react";
import type { Config, Data, Layout } from "plotly.js";

import type { Analysis } from "../api/client";
import { StatusBanner } from "./StatusBanner";

const Plot = lazy(() => import("react-plotly.js"));

interface AnalysisDashboardProps {
  analysis: Analysis;
  downloadBusy: boolean;
  onDownloadReport: (format: "markdown" | "pdf") => Promise<void>;
}

interface PlotlyFigure {
  data?: Data[];
  layout?: Partial<Layout>;
  config?: Partial<Config>;
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
    <div className="table-scroll">
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

export function AnalysisDashboard({
  analysis,
  downloadBusy,
  onDownloadReport,
}: AnalysisDashboardProps) {
  return (
    <section className="panel analysis-panel" aria-labelledby="analysis-results-heading">
      <div className="section-heading">
        <span className="step-number" aria-hidden="true">4</span>
        <div>
          <p className="eyebrow">Deterministic results</p>
          <h2 id="analysis-results-heading">Analysis dashboard</h2>
        </div>
      </div>

      <StatusBanner kind="info">
        Anomaly flags are review candidates, not confirmed fraud or misconduct. This workbench
        supports human decisions; it does not make autonomous decisions.
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
          <strong>{analysis.anomaly.flagged_count.toLocaleString()}</strong>
        </div>
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
          {analysis.charts.map((chart) => {
            const figure = chart.figure as PlotlyFigure;
            return (
              <article className="chart-card" key={`${chart.title}-${chart.chart_type}`}>
                <h3>{chart.title}</h3>
                <p>{chart.description}</p>
                <Suspense fallback={<p className="chart-loading">Loading chart…</p>}>
                  <Plot
                    data={figure.data ?? []}
                    layout={{
                      ...figure.layout,
                      autosize: true,
                      paper_bgcolor: "transparent",
                      plot_bgcolor: "transparent",
                      font: { ...(figure.layout?.font ?? {}), color: "#cbd8ea" },
                    }}
                    config={{ ...figure.config, displaylogo: false, responsive: true }}
                    useResizeHandler
                    style={{ height: "360px", width: "100%" }}
                  />
                </Suspense>
              </article>
            );
          })}
        </div>
      ) : null}

      <div className="results-grid">
        <article className="result-card">
          <h3>Anomaly review</h3>
          <dl className="definition-grid">
            <div><dt>Method</dt><dd>{analysis.anomaly.method}</dd></div>
            <div><dt>Model flagged</dt><dd>{analysis.anomaly.model_flagged_count}</dd></div>
            <div><dt>Rule flagged</dt><dd>{analysis.anomaly.rule_flagged_count}</dd></div>
            <div><dt>Features</dt><dd>{analysis.anomaly.feature_columns.join(", ") || "None"}</dd></div>
          </dl>
          {analysis.anomaly.limitations.map((limitation) => (
            <p className="fine-print" key={limitation}>{limitation}</p>
          ))}
          <RecordsTable caption="Rows flagged for review" records={analysis.anomaly.flagged_rows} />
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
