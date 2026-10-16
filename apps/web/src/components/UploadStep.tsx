import { useId, useRef } from "react";

interface UploadStepProps {
  disabled: boolean;
  busy: boolean;
  sheets: string[];
  /** Selected file held by the parent, so it survives the section being unmounted. */
  file: File | null;
  sheet: string;
  onFileChange: (file: File | null) => void;
  onSheetChange: (sheet: string) => void;
  onUpload: (file: File) => Promise<unknown>;
  onSelectSheet: (sheet: string) => Promise<void>;
}

export function UploadStep({
  disabled,
  busy,
  sheets,
  file,
  sheet,
  onFileChange,
  onSheetChange,
  onUpload,
  onSelectSheet,
}: UploadStepProps) {
  const fileId = useId();
  const selectionId = useId();
  const sheetId = useId();
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <section className="panel upload-panel" aria-labelledby="upload-heading">
      <div className="section-heading">
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
          ref={inputRef}
          type="file"
          accept=".csv,.xls,.xlsx"
          aria-describedby={file ? selectionId : undefined}
          disabled={disabled || busy}
          onChange={(event) => onFileChange(event.target.files?.[0] ?? null)}
        />
        <span className="field-hint">Accepted formats: .csv, .xls, .xlsx</span>
        {/* A browser cannot put a file back into a remounted file input, so the kept selection is
            named here instead. Choosing a file replaces it. */}
        {file ? (
          <span className="selected-files" id={selectionId}>
            Selected file: <strong>{file.name}</strong>
            <button
              className="button button--link"
              type="button"
              disabled={disabled || busy}
              onClick={() => {
                onFileChange(null);
                if (inputRef.current) inputRef.current.value = "";
              }}
            >
              Clear selection
            </button>
          </span>
        ) : null}
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
              onChange={(event) => onSheetChange(event.target.value)}
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
