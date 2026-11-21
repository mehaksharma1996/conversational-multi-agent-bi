# UI-5: documents, retrieval, and source management (#153)

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/153
Tier: medium-high. Check mode: `--full` (API and web changes).

**Status (2026-11-16):** U1 approved (whole-collection removal). S1 (API) and S2 (web) implemented
locally with mocked and offline evidence. Deliberately kept: the existing visible copy ("Index PDF
guidance", "Upload and index PDFs", the "Indexed N document(s)" banner), because scripted real-stack
journeys depend on it. Not added: a per-file `documents` breakdown (the indexer reports totals only)
and browser-side size limits (no PDF capabilities endpoint exists; the server stays authoritative).

## Goal

The document stage of the workbench meets the UI-2/UI-3 bar: a person can choose PDFs, understand
the server's limits and the indexing progress, see exactly which sources answers may cite, replace or
remove them safely, and recover from failures without guessing what the server did.

## Baseline (verified against `3f53d70` on 2026-11-16)

- API: `POST /workspaces/{id}/document-collections` (multipart, synchronous indexing in a worker
  thread) and `GET /document-collections/{id}`. `DocumentCollectionResponse` has aggregate
  `document_count`, `page_count`, `chunk_count`, and `filenames`. There is no list or delete route.
- A workspace has one collection. Indexing again replaces it; the UI already confirms replacement
  ("Replace the indexed documents?") and runs in the UI-2 mutation lane with stale-response checks.
- **The original PDFs are not retained.** The record keeps filenames, SHA-256 hashes, counts, and the
  index (Chroma under the workspace directory, or pgvector rows scoped by tenant and workspace).
  Removing one document from a collection would therefore require re-uploading the others.
- `apps/web/src/components/DocumentUpload.tsx` (89 lines) is a single form with one success banner.

## Decision U1 (needs approval): what "deletion" means

- **Approved 2026-11-16: remove the whole collection.** `DELETE /document-collections/{id}` (capability
  `data:write`, tenant-scoped; 204 once, then 404 like a foreign or unknown ID, so existence is
  never revealed). It closes the retriever,
  deletes the collection's index data (Chroma directory or pgvector scope rows) and its stored
  record, and audits `documents.deleted` (content-free: counts only). Conversations bound to the
  collection are cleared from the workbench, as replacement already does. This is honest about the
  index being built as one unit.
- Alternative: per-document removal by re-indexing. It needs the remaining PDFs re-uploaded (they
  are not stored) and doubles indexing cost; not recommended.
- Alternative: no deletion route; "replace" only. This leaves no way to stop answers citing documents
  short of deleting the workspace.

## Implementation (after U1)

**S1 (API, additive).** The delete route above, with a repository method that removes index data
for both backends and a regression test proving that retrieval returns nothing afterwards. An
additive `documents` list on the response (`filename`, `page_count`, `chunk_count`) if the indexer
can report it per file without re-reading content; otherwise keep the aggregates. Regenerate the
OpenAPI document; the compatibility check must pass.

**S2 (web).** Lazy-loaded document stage:
1. Selection with the server's limits (size, page cap) checked before submission, and the chosen
   files listed and removable before upload.
2. Indexing in the mutation lane with stated stages. "Stop waiting" says that server work may still
   finish, and the overview read decides the outcome (the UI-3 pattern).
3. A source list (filenames, with counts where available), stating that answers may cite only these.
4. Replace (existing confirmation) and Remove (new danger confirmation stating consequences:
   citations from these sources disappear from new answers, the conversation is cleared, and it
   cannot be undone).
5. Errors per cause (413, 422, rate limit) with retry that keeps the selection; focus to the error,
   the source list, or the empty state after transitions.

## Tests and evidence

- pytest: delete authorization (roles, tenant isolation, foreign IDs), idempotency, index data really
  removed for Chroma and pgvector (pgvector in its CI job), audit content, OpenAPI compatibility.
- Vitest: selection validation, stages, stop waiting and reconciliation, replace and remove
  confirmations, errors and retry, focus.
- Playwright mocked with axe at desktop and 375 px; real-stack scripted document journey.
- `python -m scripts.check_all --full`, `npm run check:bundle`.

## Security and non-goals

- No raw document text, filenames, or hashes in logs, telemetry, audit, browser storage, or operation
  markers (filenames are already shown to the owning user and stay out of telemetry).
- No change to retrieval quality, chunking, embeddings, citations, or grounding checks; no evaluation
  baseline change.
- No resumable upload, background indexing jobs, or OCR.
