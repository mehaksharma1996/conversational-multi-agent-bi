import { useState } from "react";

const PREVIEW_ROWS = 25;

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  switch (typeof value) {
    case "number":
      return value.toLocaleString(undefined, { maximumFractionDigits: 6 });
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

/**
 * The exact rows the query returned. Large results show a preview first so the page stays
 * responsive; the full set is one button away and in the CSV and Excel exports.
 */
export function ResultTable({
  rows,
  label,
}: {
  rows: Record<string, unknown>[];
  /** Distinguishes this table from the others on the page for assistive technology. */
  label: string;
}) {
  const [showAll, setShowAll] = useState(false);
  const columns = Array.from(new Set(rows.flatMap((row) => Object.keys(row))));
  const visible = showAll ? rows : rows.slice(0, PREVIEW_ROWS);
  const numeric = new Set(
    columns.filter((column) =>
      rows.every((row) => row[column] === null || row[column] === undefined || typeof row[column] === "number"),
    ),
  );
  return (
    <div className="result-table">
      <div
        className="table-scroll"
        role="region"
        aria-label={label}
        tabIndex={0}
      >
        <table>
          <caption>
            Query result: {rows.length.toLocaleString()} row{rows.length === 1 ? "" : "s"}
            {rows.length > visible.length ? ` (showing the first ${visible.length})` : ""}
          </caption>
          <thead>
            <tr>
              {columns.map((column) => (
                <th scope="col" key={column} className={numeric.has(column) ? "is-numeric" : undefined}>
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, index) => (
              <tr key={index}>
                {columns.map((column) => (
                  <td key={column} className={numeric.has(column) ? "is-numeric" : undefined}>
                    {displayValue(row[column])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > PREVIEW_ROWS ? (
        <button
          className="button button--secondary button--compact"
          type="button"
          aria-expanded={showAll}
          onClick={() => setShowAll((current) => !current)}
        >
          {showAll ? `Show first ${PREVIEW_ROWS} rows` : `Show all ${rows.length.toLocaleString()} rows`}
        </button>
      ) : null}
    </div>
  );
}
