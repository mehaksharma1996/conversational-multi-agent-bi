import { useId, useState } from "react";

interface UploadStepProps {
  disabled: boolean;
  busy: boolean;
  sheets: string[];
  onUpload: (file: File) => Promise<void>;
  onSelectSheet: (sheet: string) => Promise<void>;
}

export function UploadStep({
  disabled,
  busy,
  sheets,
  onUpload,
  onSelectSheet,
}: UploadStepProps) {
  const fileId = useId();
  const sheetId = useId();
  const [file, setFile] = useState<File | null>(null);
  const [sheet, setSheet] = useState("");

  return (
    <section className="panel upload-panel" aria-labelledby="upload-heading">
      <div className="section-heading">
        <span className="step-number" aria-hidden="true">
          1
        </span>
        <div>
          <p className="eyebrow">Source data</p>
          <h2 id="upload-heading">Upload a business dataset</h2>
        </div>
      </div>
      <p className="section-copy">
        CSV and Excel files are normalized and profiled locally. The server enforces upload and
        row limits before analysis.
      </p>

      <div className="upload-control">
        <label htmlFor={fileId}>CSV or Excel file</label>
        <input
          id={fileId}
          type="file"
          accept=".csv,.xls,.xlsx"
          disabled={disabled || busy}
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        <span className="field-hint">Accepted formats: .csv, .xls, .xlsx</span>
      </div>

      <button
        className="button button--primary"
        type="button"
        disabled={disabled || busy || file === null}
        onClick={() => file && void onUpload(file)}
      >
        {busy ? "Processing…" : "Upload and profile"}
      </button>

      {sheets.length > 0 ? (
        <div className="sheet-picker">
          <label htmlFor={sheetId}>Workbook sheet</label>
          <div className="inline-control">
            <select
              id={sheetId}
              value={sheet}
              disabled={busy}
              onChange={(event) => setSheet(event.target.value)}
            >
              <option value="">Select a sheet</option>
              {sheets.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
            <button
              className="button button--secondary"
              type="button"
              disabled={busy || sheet === ""}
              onClick={() => void onSelectSheet(sheet)}
            >
              Profile selected sheet
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
