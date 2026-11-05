import { useId, useRef, useState } from "react";

import type { TabularCapabilities } from "../api/client";
import type { ActivePhase, OperationProgress } from "../operations/model";

export interface DataOperationView {
  phase: ActivePhase;
  progress: OperationProgress | null;
  cancellable: boolean;
}

interface UploadStepProps {
  disabled: boolean;
  capabilities: TabularCapabilities | null;
  capabilitiesUnavailable: boolean;
  operation: DataOperationView | null;
  sheets: string[];
  /** Selected file held by the parent, so it survives the section being unmounted. */
  file: File | null;
  sheet: string;
  onFileChange: (file: File | null) => void;
  onSheetChange: (sheet: string) => void;
  onUpload: (file: File) => Promise<unknown>;
  onSelectSheet: (sheet: string) => Promise<void>;
  onCancel: () => void;
  onRetryCapabilities: () => void;
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toLocaleString(undefined, { maximumFractionDigits: 1 })} MB`;
  }
  return `${Math.ceil(bytes / 1024).toLocaleString()} KB`;
}

function validateTabularFile(file: File, capabilities: TabularCapabilities): string | null {
  const dot = file.name.lastIndexOf(".");
  const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (!capabilities.supported_extensions.includes(extension)) {
    return `Choose ${capabilities.supported_extensions.join(", ")} file.`;
  }
  if (file.size > capabilities.max_upload_bytes) {
    return `${file.name} is larger than the ${formatBytes(capabilities.max_upload_bytes)} server limit.`;
  }
  return null;
}

export function UploadStep({
  disabled,
  capabilities,
  capabilitiesUnavailable,
  operation,
  sheets,
  file,
  sheet,
  onFileChange,
  onSheetChange,
  onUpload,
  onSelectSheet,
  onCancel,
  onRetryCapabilities,
}: UploadStepProps) {
  const fileId = useId();
  const constraintsId = useId();
  const selectionId = useId();
  const validationId = useId();
  const sheetId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [validation, setValidation] = useState<string | null>(null);
  const extensions = capabilities?.supported_extensions ?? [".csv", ".xls", ".xlsx"];

  const selectFile = (next: File | null) => {
    const problem = next && capabilities ? validateTabularFile(next, capabilities) : null;
    setValidation(problem);
    onFileChange(problem ? null : next);
  };

  return (
    <section className="panel upload-panel" aria-labelledby="upload-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">1 · Select and profile</p>
          <h2 id="upload-heading">Upload a business dataset</h2>
        </div>
      </div>
      <p className="section-copy">
        The server accepts a file, inspects workbook sheets when needed, then creates a private
        profile for schema review. Browser checks below are guidance; the server enforces every
        format, size, row, and workspace boundary.
      </p>

      <div className="upload-constraints" id={constraintsId}>
        {capabilities ? (
          <>
            <strong>{extensions.join(", ")}</strong>
            <span>Up to {formatBytes(capabilities.max_upload_bytes)}</span>
            <span>Up to {capabilities.max_rows.toLocaleString()} rows</span>
          </>
        ) : capabilitiesUnavailable ? (
          <>
            <span>Active size and row limits are unavailable. The server will validate the file.</span>
            <button className="button button--link" type="button" onClick={onRetryCapabilities}>
              Retry limits
            </button>
          </>
        ) : (
          <span>Loading active server limits…</span>
        )}
      </div>

      <div className="upload-control">
        <label htmlFor={fileId}>CSV or Excel file</label>
        <input
          id={fileId}
          ref={inputRef}
          type="file"
          accept={extensions.join(",")}
          aria-describedby={[constraintsId, validation ? validationId : null, file ? selectionId : null]
            .filter(Boolean)
            .join(" ")}
          aria-invalid={validation ? true : undefined}
          disabled={disabled}
          onChange={(event) => selectFile(event.target.files?.[0] ?? null)}
        />
        {validation ? (
          <span className="field-error" id={validationId} role="alert">
            {validation}
          </span>
        ) : null}
        {file ? (
          <span className="selected-files" id={selectionId}>
            Selected file: <strong>{file.name}</strong> ({formatBytes(file.size)})
            <button
              className="button button--link"
              type="button"
              disabled={disabled}
              onClick={() => {
                setValidation(null);
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
        disabled={disabled || file === null}
        onClick={() => file && void onUpload(file)}
      >
        {operation ? "Processing…" : "Upload and profile"}
      </button>

      {operation ? (
        <div className="data-operation" aria-live="polite">
          <strong>{operation.progress?.stage ?? "Preparing data…"}</strong>
          {operation.progress?.total ? (
            <progress
              aria-label="Data setup progress"
              max={operation.progress.total}
              value={operation.progress.completed}
            />
          ) : null}
          <span>
            {operation.phase === "cancelling"
              ? "Stopping this browser request and checking the workspace…"
              : "Keep this page open while the server responds."}
          </span>
          {operation.cancellable && operation.phase !== "cancelling" ? (
            <button className="button button--secondary" type="button" onClick={onCancel}>
              Stop waiting and check
            </button>
          ) : null}
          {operation.cancellable ? (
            <small>Stopping the browser request cannot promise that server work stopped.</small>
          ) : null}
        </div>
      ) : null}

      {sheets.length > 0 ? (
        <div className="sheet-picker">
          <label htmlFor={sheetId}>Workbook sheet</label>
          <p className="field-hint">Choose the sheet to profile. No dataset is replaced until profiling succeeds.</p>
          <div className="inline-control">
            <select
              id={sheetId}
              value={sheet}
              disabled={disabled}
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
              disabled={disabled || sheet === ""}
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
