import { useMemo, useState } from "react";

import type { Dataset, SchemaMappingUpdate } from "../api/client";
import { StatusBanner } from "./StatusBanner";

const CANONICAL_FIELDS = [
  "amount",
  "date",
  "customer_id",
  "merchant",
  "location",
  "label",
] as const;

type CanonicalField = (typeof CANONICAL_FIELDS)[number];
type MappingDraft = Record<CanonicalField, string>;

const REQUIRED_TYPES: Record<CanonicalField, string[]> = {
  amount: ["numeric"],
  date: ["date"],
  customer_id: ["categorical", "text", "numeric"],
  merchant: ["categorical", "text"],
  location: ["categorical", "text"],
  label: ["categorical", "boolean", "numeric"],
};

function initialMapping(dataset: Dataset): MappingDraft {
  return Object.fromEntries(
    CANONICAL_FIELDS.map((field) => {
      const suggestion = dataset.schema_mapping.mappings[field];
      const source = suggestion && suggestion.confidence >= 0.5 ? suggestion.source_column : null;
      return [field, source ?? ""];
    }),
  ) as MappingDraft;
}

interface SchemaReviewProps {
  dataset: Dataset;
  busy: boolean;
  onConfirm: (mapping: SchemaMappingUpdate) => Promise<void>;
}

export function SchemaReview({ dataset, busy, onConfirm }: SchemaReviewProps) {
  const [mapping, setMapping] = useState<MappingDraft>(() => initialMapping(dataset));
  const lowConfidenceFields = useMemo(
    () =>
      CANONICAL_FIELDS.filter((field) => {
        const suggestion = dataset.schema_mapping.mappings[field];
        return Boolean(
          suggestion?.source_column && suggestion.confidence > 0 && suggestion.confidence < 0.5,
        );
      }),
    [dataset],
  );

  const submit = () => {
    const payload = Object.fromEntries(
      CANONICAL_FIELDS.map((field) => [field, mapping[field] || null]),
    ) as SchemaMappingUpdate;
    void onConfirm(payload);
  };

  return (
    <section className="panel" aria-labelledby="schema-heading">
      <div className="section-heading">
        <span className="step-number" aria-hidden="true">
          2
        </span>
        <div>
          <p className="eyebrow">Human review</p>
          <h2 id="schema-heading">Confirm the canonical schema</h2>
        </div>
      </div>

      <div className="metrics-grid" aria-label="Dataset summary">
        <div className="metric-card">
          <span>Rows</span>
          <strong>{dataset.row_count.toLocaleString()}</strong>
        </div>
        <div className="metric-card">
          <span>Columns</span>
          <strong>{dataset.column_count.toLocaleString()}</strong>
        </div>
        <div className="metric-card">
          <span>Duplicates</span>
          <strong>{dataset.profile.duplicate_row_count.toLocaleString()}</strong>
        </div>
        <div className="metric-card">
          <span>Status</span>
          <strong>{dataset.status === "ready" ? "Confirmed" : "Review required"}</strong>
        </div>
      </div>

      {Object.entries(dataset.column_warnings).map(([column, warnings]) =>
        warnings.map((warning) => (
          <StatusBanner kind="info" key={`${column}-${warning}`}>
            <strong>{column}:</strong> {warning}
          </StatusBanner>
        )),
      )}

      {lowConfidenceFields.length > 0 ? (
        <StatusBanner kind="info">
          Low-confidence suggestions are not selected automatically: {lowConfidenceFields.join(", ")}.
          Choose them only after reviewing the column profile.
        </StatusBanner>
      ) : null}

      <div className="schema-grid">
        {CANONICAL_FIELDS.map((field) => {
          const options = dataset.profile.columns.filter((column) =>
            REQUIRED_TYPES[field].includes(column.inferred_type),
          );
          const suggestion = dataset.schema_mapping.mappings[field];
          return (
            <div className="field-group" key={field}>
              <label htmlFor={`mapping-${field}`}>{field.replaceAll("_", " ")}</label>
              <select
                id={`mapping-${field}`}
                value={mapping[field]}
                disabled={busy}
                onChange={(event) =>
                  setMapping((current) => ({ ...current, [field]: event.target.value }))
                }
              >
                <option value="">Not mapped</option>
                {options.map((column) => (
                  <option key={column.name} value={column.name}>
                    {column.name} · {column.inferred_type}
                  </option>
                ))}
              </select>
              <span className="field-hint">
                {suggestion?.source_column
                  ? `Suggested ${suggestion.source_column} · ${Math.round(suggestion.confidence * 100)}% confidence`
                  : "No compatible suggestion found"}
              </span>
            </div>
          );
        })}
      </div>

      <details className="profile-table-wrap">
        <summary>Inspect column profile</summary>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Column</th>
                <th scope="col">Type</th>
                <th scope="col">Missing</th>
                <th scope="col">Unique</th>
                <th scope="col">Examples</th>
              </tr>
            </thead>
            <tbody>
              {dataset.profile.columns.map((column) => (
                <tr key={column.name}>
                  <th scope="row">{column.name}</th>
                  <td>{column.inferred_type}</td>
                  <td>{Math.round(column.missing_ratio * 100)}%</td>
                  <td>{column.unique_count.toLocaleString()}</td>
                  <td>{column.sample_values.join(", ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>

      <button className="button button--primary" type="button" disabled={busy} onClick={submit}>
        {busy ? "Confirming…" : "Confirm schema"}
      </button>
    </section>
  );
}
