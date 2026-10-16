import { useId, useRef } from "react";

import type { DocumentCollection } from "../api/client";
import { StatusBanner } from "./StatusBanner";

interface DocumentUploadProps {
  disabled: boolean;
  busy: boolean;
  collection: DocumentCollection | null;
  /** Selected PDFs held by the parent, so they survive the section being unmounted. */
  files: File[];
  onFilesChange: (files: File[]) => void;
  onUpload: (files: File[]) => Promise<unknown>;
}

export function DocumentUpload({
  disabled,
  busy,
  collection,
  files,
  onFilesChange,
  onUpload,
}: DocumentUploadProps) {
  const inputId = useId();
  const selectionId = useId();
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <section className="panel" aria-labelledby="documents-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Grounded document context</p>
          <h2 id="documents-heading">Index PDF guidance</h2>
        </div>
      </div>
      <p className="section-copy">
        Upload text-based PDFs to add page-aware retrieval and citations to the conversation.
        Scanned documents must be OCR-processed first.
      </p>
      <div className="upload-control">
        <label htmlFor={inputId}>PDF documents</label>
        <input
          id={inputId}
          type="file"
          accept="application/pdf,.pdf"
          multiple
          ref={inputRef}
          aria-describedby={files.length > 0 ? selectionId : undefined}
          disabled={disabled || busy}
          onChange={(event) => onFilesChange(Array.from(event.target.files ?? []))}
        />
        <span className="field-hint">The server enforces per-file, combined-size, page, and chunk limits.</span>
        {/* A browser cannot put files back into a remounted file input, so the kept selection is
            named here instead. Choosing files replaces it. */}
        {files.length > 0 ? (
          <span className="selected-files" id={selectionId}>
            Selected {files.length === 1 ? "file" : "files"}:{" "}
            <strong>{files.map((file) => file.name).join(", ")}</strong>
            <button
              className="button button--link"
              type="button"
              disabled={disabled || busy}
              onClick={() => {
                onFilesChange([]);
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
        disabled={disabled || busy || files.length === 0}
        onClick={() => void onUpload(files)}
      >
        {busy ? "Indexing documents…" : "Upload and index PDFs"}
      </button>
      {collection ? (
        <StatusBanner kind="success">
          Indexed {collection.document_count} document(s), {collection.page_count} page(s), and{" "}
          {collection.chunk_count} retrieval chunk(s): {collection.filenames.join(", ")}.
        </StatusBanner>
      ) : null}
    </section>
  );
}
