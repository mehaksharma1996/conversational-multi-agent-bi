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
  /** Removes the indexed documents after a confirmation (UI-5, #153). */
  onRemove: () => Promise<unknown>;
}

const isPdf = (file: File) => file.name.toLowerCase().endsWith(".pdf");

function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function plural(count: number, noun: string): string {
  return `${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}`;
}

export function DocumentUpload({
  disabled,
  busy,
  collection,
  files,
  onFilesChange,
  onUpload,
  onRemove,
}: DocumentUploadProps) {
  const inputId = useId();
  const selectionId = useId();
  const problemId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const rejected = files.filter((file) => !isPdf(file));
  const locked = disabled || busy;

  const replaceSelection = (next: File[]) => {
    onFilesChange(next);
    if (next.length === 0 && inputRef.current) inputRef.current.value = "";
  };

  return (
    <>
      <section className="panel" aria-labelledby="documents-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Grounded document context</p>
            <h2 id="documents-heading" tabIndex={-1}>Index PDF guidance</h2>
          </div>
        </div>
        <p className="section-copy">
          Upload text-based PDFs to add page-aware retrieval and citations to the conversation.
          Scanned documents must be OCR-processed first. The server keeps the search index, not the
          original files.
        </p>
        <div className="upload-control">
          <label htmlFor={inputId}>PDF documents</label>
          <input
            id={inputId}
            type="file"
            accept="application/pdf,.pdf"
            multiple
            ref={inputRef}
            aria-describedby={
              [files.length > 0 ? selectionId : "", rejected.length > 0 ? problemId : ""]
                .filter(Boolean)
                .join(" ") || undefined
            }
            aria-invalid={rejected.length > 0 || undefined}
            disabled={locked}
            onChange={(event) => onFilesChange(Array.from(event.target.files ?? []))}
          />
          <span className="field-hint">
            The server enforces per-file, combined-size, page, and chunk limits.
          </span>
        </div>
        {/* A browser cannot put files back into a remounted file input, so the kept selection is
            listed here instead. Choosing files replaces it. */}
        {files.length > 0 ? (
          <div className="selected-files" id={selectionId}>
            <p>Selected {files.length === 1 ? "file" : "files"}:</p>
            <ul className="file-list">
              {files.map((file, index) => (
                <li key={`${index}-${file.name}`}>
                  <span className="file-name">{file.name}</span>{" "}
                  <span className="fine-print">{size(file.size)}</span>
                  <button
                    className="button button--link"
                    type="button"
                    disabled={locked}
                    aria-label={`Remove ${file.name} from the selection`}
                    onClick={() => replaceSelection(files.filter((_, other) => other !== index))}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            <button
              className="button button--link"
              type="button"
              disabled={locked}
              onClick={() => replaceSelection([])}
            >
              Clear selection
            </button>
          </div>
        ) : null}
        {rejected.length > 0 ? (
          <p className="field-error" id={problemId} role="alert">
            Only PDF files can be indexed. Remove {rejected.map((file) => file.name).join(", ")}.
          </p>
        ) : null}
        <button
          className="button button--primary"
          type="button"
          disabled={locked || files.length === 0 || rejected.length > 0}
          onClick={() => void onUpload(files)}
        >
          {busy ? "Indexing documents…" : "Upload and index PDFs"}
        </button>
      </section>

      <section className="panel" aria-labelledby="sources-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Citable sources</p>
            <h2 id="sources-heading">Indexed documents</h2>
          </div>
        </div>
        {collection ? (
          <>
            <StatusBanner kind="success">
              Indexed {collection.document_count} document(s), {collection.page_count} page(s), and{" "}
              {collection.chunk_count} retrieval chunk(s): {collection.filenames.join(", ")}.
            </StatusBanner>
            <p className="section-copy">
              Answers may cite only these {plural(collection.document_count, "document")}:{" "}
              {plural(collection.page_count, "page")} split into{" "}
              {plural(collection.chunk_count, "retrieval chunk")}.
            </p>
            <ul className="source-list">
              {collection.filenames.map((filename, index) => (
                <li key={`${index}-${filename}`}>{filename}</li>
              ))}
            </ul>
            <button
              className="button button--danger-quiet"
              type="button"
              disabled={locked}
              onClick={() => void onRemove()}
            >
              Remove indexed documents
            </button>
          </>
        ) : (
          <p className="section-copy">
            No documents are indexed. Answers use the confirmed data only and cite no documents.
          </p>
        )}
      </section>
    </>
  );
}
