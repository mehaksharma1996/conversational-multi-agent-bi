import { useEffect, useId, useMemo, useRef, useState } from "react";

import type { Dataset, SchemaMappingUpdate } from "../api/client";
import { DEMO_DATA_FILENAME, DEMO_UNMAPPED_FIELDS } from "../demo/retailDemo";
import { focusAsApplication } from "../workspace/focus";
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
export type MappingDraft = Record<CanonicalField, string>;

const REQUIRED_TYPES: Record<CanonicalField, string[]> = {
  amount: ["numeric"],
  date: ["date"],
  customer_id: ["categorical", "text", "numeric"],
  merchant: ["categorical", "text"],
  location: ["categorical", "text"],
  label: ["categorical", "boolean", "numeric"],
};

function intentionallyUnmapped(dataset: Dataset): Record<string, string> {
  return dataset.filename === DEMO_DATA_FILENAME ? { ...DEMO_UNMAPPED_FIELDS } : {};
}

function initialMapping(dataset: Dataset): MappingDraft {
  const unmapped = intentionallyUnmapped(dataset);
  return Object.fromEntries(
    CANONICAL_FIELDS.map((field) => {
      if (field in unmapped) return [field, ""];
      const suggestion = dataset.schema_mapping.mappings[field];
      const source = suggestion && suggestion.confidence >= 0.5 ? suggestion.source_column : null;
      return [field, source ?? ""];
    }),
  ) as MappingDraft;
}

function duplicateSources(mapping: MappingDraft): Set<string> {
  const counts = new Map<string, number>();
  for (const source of Object.values(mapping)) {
    if (source) counts.set(source, (counts.get(source) ?? 0) + 1);
  }
  return new Set([...counts].filter(([, count]) => count > 1).map(([source]) => source));
}

interface SchemaReviewProps {
  dataset: Dataset;
  busy: boolean;
  /** Edits held by the parent for this dataset; null starts from the suggested mapping. */
  draft: MappingDraft | null;
  onDraftChange: (mapping: MappingDraft) => void;
  onConfirm: (mapping: SchemaMappingUpdate) => Promise<void>;
  focusOnMount?: boolean;
  onFocused?: () => void;
}

export function SchemaReview({
  dataset,
  busy,
  draft,
  onDraftChange,
  onConfirm,
  focusOnMount = false,
  onFocused,
}: SchemaReviewProps) {
  const suggested = useMemo(() => initialMapping(dataset), [dataset]);
  const mapping = draft ?? suggested;
  const unmapped = useMemo(() => intentionallyUnmapped(dataset), [dataset]);
  const [profileQuery, setProfileQuery] = useState("");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const mappingErrorId = useId();
  const duplicates = useMemo(() => duplicateSources(mapping), [mapping]);
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
  const missingColumns = dataset.profile.columns.filter((column) => column.missing_count > 0);
  const missingCells = missingColumns.reduce((total, column) => total + column.missing_count, 0);
  const warningCount = Object.values(dataset.column_warnings).reduce(
    (total, warnings) => total + warnings.length,
    0,
  );
  const visibleColumns = dataset.profile.columns.filter((column) =>
    column.name.toLocaleLowerCase().includes(profileQuery.trim().toLocaleLowerCase()),
  );

  useEffect(() => {
    if (!focusOnMount) return;
    focusAsApplication(headingRef.current);
    onFocused?.();
  }, [focusOnMount, onFocused]);

  const submit = () => {
    if (duplicates.size > 0) return;
    const payload = Object.fromEntries(
      CANONICAL_FIELDS.map((field) => [field, mapping[field] || null]),
    ) as SchemaMappingUpdate;
    void onConfirm(payload);
  };

  return (
    <section className="panel schema-review" aria-labelledby="schema-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">2 · Review and confirm</p>
          <h2 id="schema-heading" ref={headingRef} tabIndex={-1}>
            Confirm the canonical schema
          </h2>
        </div>
        <span className="status-chip status-chip--awaiting_approval">Review required</span>
      </div>
      <p className="section-copy">
        Confirm only the business meanings this dataset supports. Unmapped fields remain available
        as source columns but do not gain a canonical meaning.
      </p>

      <div className="metrics-grid" aria-label="Dataset profile summary">
        <div className="metric-card">
          <span>Rows</span>
          <strong>{dataset.row_count.toLocaleString()}</strong>
        </div>
        <div className="metric-card">
          <span>Columns</span>
          <strong>{dataset.column_count.toLocaleString()}</strong>
        </div>
        <div className="metric-card">
          <span>Missing cells</span>
          <strong>{missingCells.toLocaleString()}</strong>
          <small>{missingColumns.length.toLocaleString()} affected columns</small>
        </div>
        <div className="metric-card">
          <span>Duplicate rows</span>
          <strong>{dataset.profile.duplicate_row_count.toLocaleString()}</strong>
        </div>
        <div className="metric-card">
          <span>Type warnings</span>
          <strong>{warningCount.toLocaleString()}</strong>
        </div>
      </div>

      {warningCount === 0 && missingCells === 0 && dataset.profile.duplicate_row_count === 0 ? (
        <StatusBanner kind="success">No missing values, duplicate rows, or type warnings were detected.</StatusBanner>
      ) : null}

      {Object.entries(dataset.column_warnings).map(([column, warnings]) =>
        warnings.map((warning) => (
          <StatusBanner kind="info" key={`${column}-${warning}`}>
            <strong>{column}:</strong> {warning}
          </StatusBanner>
        )),
      )}

      {Object.entries(unmapped).map(([field, reason]) => (
        <StatusBanner kind="info" key={`unmapped-${field}`}>
          <strong>{field.replaceAll("_", " ")} is left unmapped for the demo:</strong> {reason}
        </StatusBanner>
      ))}

      {lowConfidenceFields.length > 0 ? (
        <StatusBanner kind="info">
          Low-confidence suggestions are not selected automatically: {lowConfidenceFields.join(", ")}.
          Choose them only after reviewing the column profile.
        </StatusBanner>
      ) : null}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <fieldset disabled={busy}>
          <legend>Canonical business fields</legend>
          <div className="schema-grid">
            {CANONICAL_FIELDS.map((field) => {
              const options = dataset.profile.columns.filter((column) =>
                REQUIRED_TYPES[field].includes(column.inferred_type),
              );
              const suggestion = dataset.schema_mapping.mappings[field];
              const hintId = `mapping-${field}-hint`;
              const duplicate = duplicates.has(mapping[field]);
              return (
                <div className="field-group" key={field}>
                  <label htmlFor={`mapping-${field}`}>{field.replaceAll("_", " ")}</label>
                  <select
                    id={`mapping-${field}`}
                    value={mapping[field]}
                    aria-invalid={duplicate || undefined}
                    aria-describedby={`${hintId}${duplicate ? ` ${mappingErrorId}` : ""}`}
                    onChange={(event) =>
                      onDraftChange({ ...mapping, [field]: event.target.value })
                    }
                  >
                    <option value="">Not mapped</option>
                    {options.map((column) => (
                      <option key={column.name} value={column.name}>
                        {column.name} · {column.inferred_type}
                      </option>
                    ))}
                  </select>
                  <span className="field-hint" id={hintId}>
                    {suggestion?.source_column
                      ? `Suggested ${suggestion.source_column} · ${Math.round(suggestion.confidence * 100)}% confidence. ${suggestion.reason}`
                      : "No compatible suggestion found."}
                  </span>
                </div>
              );
            })}
          </div>
        </fieldset>

        {duplicates.size > 0 ? (
          <p className="field-error" id={mappingErrorId} role="alert">
            Each source column can represent only one canonical field. Review: {[...duplicates].join(", ")}.
          </p>
        ) : null}

        <details className="profile-table-wrap" open>
          <summary>Column profile and data quality</summary>
          <div className="profile-filter">
            <label htmlFor="profile-column-filter">Filter columns</label>
            <input
              id="profile-column-filter"
              type="search"
              value={profileQuery}
              onChange={(event) => setProfileQuery(event.target.value)}
            />
            <span className="field-hint" role="status">
              Showing {visibleColumns.length.toLocaleString()} of {dataset.profile.columns.length.toLocaleString()} columns
            </span>
          </div>
          <div className="table-scroll" role="region" tabIndex={0} aria-label="Column profile">
            <table>
              <caption>Inferred types, completeness, uniqueness, and sample values</caption>
              <thead>
                <tr>
                  <th scope="col">Column</th>
                  <th scope="col">Stored type</th>
                  <th scope="col">Inferred meaning</th>
                  <th scope="col">Missing</th>
                  <th scope="col">Unique</th>
                  <th scope="col">Examples</th>
                </tr>
              </thead>
              <tbody>
                {visibleColumns.map((column) => (
                  <tr key={column.name}>
                    <th scope="row">{column.name}</th>
                    <td>{column.dtype}</td>
                    <td>{column.inferred_type}</td>
                    <td>
                      {column.missing_count.toLocaleString()} ({Math.round(column.missing_ratio * 100)}%)
                    </td>
                    <td>
                      {column.unique_count.toLocaleString()} ({Math.round(column.unique_ratio * 100)}%)
                    </td>
                    <td>{column.sample_values.join(", ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {visibleColumns.length === 0 ? <p className="empty-state">No columns match this filter.</p> : null}
          </div>
        </details>

        <button className="button button--primary" type="submit" disabled={busy || duplicates.size > 0}>
          {busy ? "Confirming…" : "Confirm schema"}
        </button>
      </form>
    </section>
  );
}
