import { useId, useState } from "react";

import type { DocumentCollection } from "../api/client";
import { StatusBanner } from "./StatusBanner";

interface DocumentUploadProps {
  disabled: boolean;
  busy: boolean;
  collection: DocumentCollection | null;
  onUpload: (files: File[]) => Promise<void>;
}

export function DocumentUpload({
  disabled,
  busy,
  collection,
  onUpload,
}: DocumentUploadProps) {
  const inputId = useId();
  const [files, setFiles] = useState<File[]>([]);

  return (
    <section className="panel" aria-labelledby="documents-heading">
      <div className="section-heading">
        <span className="step-number" aria-hidden="true">5</span>
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
          disabled={disabled || busy}
          onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
        />
        <span className="field-hint">The server enforces per-file, combined-size, page, and chunk limits.</span>
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
