# Brief: #132 UI-2 operation state, feedback, safe resource replacement, and recovery

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/132
Tier: strongest. Check mode: --full.

## Outcome

The workbench tracks each operation separately as a typed value (`idle → queued → running
(progress) → cancelling → terminal`) instead of a single global `task` value. One operation's
completion can no longer clear another's busy state. Status changes are announced through one
persistent live region.

- **Replacement.** Replacing a dataset or document collection first asks for confirmation in an
  accessible dialog. The current state stays visible until the replacement succeeds, after which
  dependent results are cleared. A failed replacement leaves the earlier state intact.
- **Reload.** A reload during an operation tells the user what was interrupted and shows the
  server's current state.
- **Jobs.** Tenant-scoped jobs are polled with backoff, can be cancelled where the API allows it,
  and are reported as interrupted (outcome unknown) when an API restart erases them. This is the
  base that UI-6b's investigation progress builds on.

## Baseline and worktree

- Base: `origin/main` at `7080ecc` (PR #196 merged), verified on 2026-10-29.
- Worktree: `work/agent-worktrees/132-ui2-operation-state-recovery`, branch
  `feat/iteration2-ui2-operation-state-recovery`.
- Stage 1 shipped in PR #130 (`docs/architecture/workbench-shell.md`). Relevant ADRs: 0003 (React
  and TypeScript frontend), 0005 and 0020 (job boundary, in-process execution, process-local jobs),
  and 0011 (React/FastAPI is the primary product).

## Facts (verified against `7080ecc` on 2026-10-29)

**Operation state today (`apps/web/src/App.tsx`)**
- One `task: Task | null` value covers ten operations: `workspace`, `upload`, `dataset`,
  `schema`, `analysis`, `documents`, `consent`, `conversation`, `download`, `reset`.
- Every handler runs `setTask(x)`, then `await`, then `setTask(null)` in `finally`.
- `handleQuestion` and `handleApproval` refuse to start while `task !== null`; the download and
  export handlers have no guard of their own.
- Today this is safe only because every control is disabled while any task runs, and React
  re-renders between discrete clicks (verified: rapid double clicks start one request even on the
  old code). The single value can't represent two overlapping operations, which UI-6b's jobs and
  the read lane need (F1, latent).

**Every product operation is one synchronous HTTP request**
- That covers upload, profiling, schema confirmation, analysis, document indexing, questions,
  approvals, and exports.
- Only `apps/api/investigations.py` submits background jobs, through `InProcessJobExecutor`, and
  investigation routing is off by default.
- `/api/v1/jobs` provides list, get, DELETE (best-effort cancel), and retry. All are tenant-scoped,
  content-free, and `Cache-Control: no-store`.
- Job statuses are `queued | running | succeeded | failed | cancelled | expired`, with
  `progress_stage`, `progress_completed`, `progress_total`, `cancel_requested`, and `retryable`.
- `InMemoryJobStore` is process-local (ADR 0020): after an API restart a job ID returns 404. There
  is no `interrupted` job status; only investigation *records* are durable and become
  `interrupted`.

**Idempotency**
- Only `POST /workspaces` and `POST /conversations/{id}/investigations` accept `Idempotency-Key`.
- Uploads, datasets, analyses, document collections, conversations, messages, reports, and exports
  are not idempotent, so retrying them automatically could duplicate work.
- `api/client.ts` aborts after 120 s (`timeout`). `ApiClientError.retryable` covers network,
  timeout, and 5xx errors.

**Replacement semantics on the server (`apps/api/repository.py`)**
- Datasets and analyses are additive, and the workspace overview returns the *latest* of each.
  Older ones remain until the workspace expires.
- A document upload **deletes every previous collection** in the workspace (`retriever.close()`
  and `replace_document_collection`), but only after the new one has been indexed.
- A conversation is bound to the dataset and collection it was created with.
  `workspace/recovery.ts` already drops a conversation that no longer matches.

**Current replacement UX**
- `handleUpload` clears `dataset`, `analysis`, and the conversation *before* the upload request.
  A failed upload therefore blanks the screen while the server still has the old dataset, and a
  reload brings it back (F2).
- Document upload replaces the collection with no confirmation and no warning that the previous
  documents will be deleted (F3).
- Schema confirmation and re-running analysis silently discard the conversation, including any
  pending SQL approvals (F4). `handleReset` uses `window.confirm` (F5).

**Recovery**
- `recoverWorkspace()` rebuilds state from the server after a reload: a sessionStorage hint, then
  `listWorkspaces`, then the overview.
- It doesn't know whether an operation was in flight when the page reloaded, so the UI can't say
  that an upload or indexing request may not have completed (F6).
- Drafts (`workspace/drafts.ts`) are memory-only by policy.

**Feedback and accessibility**
- `StatusBanner` uses `role="alert"` for errors and `role="status"` otherwise, but banners mount
  and unmount, so some screen readers miss announcements (F7).
- `ConversationPanel` has its own polite live region. Busy state is shown through button labels
  ("Running analysis…").
- `styles.css` has a `prefers-reduced-motion` block and 44 px controls.
- Coverage: `e2e/accessibility.spec.ts` runs axe baselines; Vitest uses React Testing Library and
  `user-event`; mocked Playwright specs are in `e2e/*.spec.ts`; scripted real-stack journeys are
  in `e2e/real/` (`E2E_REAL_STACK=scripted`, production API with a scripted provider).

## Failure modes this fixes

| ID | Failure the user sees | Fix |
|---|---|---|
| F1 (latent) | One `task` value can't represent overlapping operations; any future overlap (a job and a download) would let the first to finish clear the other's busy state | Per-operation records; no shared `finally` |
| F2 | A failed upload blanks the dataset, analysis, and conversation that the server still has | Keep prior state until success (D4) |
| F3 | Uploading documents silently deletes the previous collection | Confirmation dialog naming what is lost (D5) |
| F4 | Pending SQL approvals vanish on re-analysis or schema confirmation, without warning | The confirmation lists pending approvals that will be abandoned |
| F5 | Reset uses the browser's `window.confirm`, which cannot be styled, tested reliably, or given consistent focus handling | Shared `ConfirmDialog` |
| F6 | After a reload mid-operation, nothing says the operation may not have finished | Content-free in-flight marker (D6) |
| F7 | Status changes are not reliably announced | One persistent live region (D8) |
| F8 | No way to follow or cancel a background job | `useJob` polling hook (D2, D7) |

## Design decisions (S0)

Decisions marked **[approval]** change behaviour or scope and need maintainer sign-off before
implementation. The others follow from existing contracts.

**Maintainer approval (2026-10-29).** D2 and R1 are approved as proposed: the hook ships now, and
real-stack job coverage comes with UI-6b. D4 and D5 are approved as written. D6 is approved, with
the marker holding only the operation kind, the workspace ID, and the start time.

- **D1. Operation model.** Add `apps/web/src/operations/` with a pure reducer and a hook. Each
  operation is `{ id, kind, lane, phase, progress?, error?, startedAt }`.
  - `phase` is `queued | running | cancelling | succeeded | failed | cancelled | interrupted`.
    `idle` means no record.
  - Completing an operation affects only its own ID. Busy flags are derived per lane, replacing
    `task !== null`.
  - It is not a second source of truth for resources: datasets and documents stay in `App` state,
    and the server stays authoritative.
- **D2 [approval]. Job scope.** No API or backend change. Every current operation stays
  synchronous; moving analysis or indexing to background jobs would be a separate backend
  milestone.
  - `useJob(jobId)` polls `GET /api/v1/jobs/{id}` with backoff: 1 s, doubling to a 10 s ceiling,
    with ±20% jitter, honouring `Retry-After`, and stopping at a terminal status.
  - It maps job statuses onto D1's phases. A 404 on a job that was being followed becomes
    `interrupted`, with the explanation "the server restarted or the job expired; its outcome is
    unknown".
  - The hook ships with Vitest and mocked-Playwright coverage, ready for UI-6b. No current screen
    starts a job.
- **D3. Lanes and concurrency.** Two lanes: `mutation` for anything that changes workspace
  resources, and `read` for downloads and exports.
  - At most one mutation runs at a time; this keeps today's guarantee and matches the
    non-idempotent API.
  - Downloads use the read lane and may overlap a mutation, but no longer clear its state.
  - A second request in the same lane is refused in the UI, not queued silently.
- **D4 [approval]. Keep prior state until success.** Upload, sheet selection, schema confirmation,
  analysis, and document indexing leave the current resources on screen while running. Dependents
  are cleared only after the new resource is returned. A failure keeps everything as it was and
  shows the error with a retry action.
- **D5 [approval]. Replacement confirmation.** A shared `ConfirmDialog` (native `<dialog>`,
  `showModal()`, focus moved to it, Escape cancels, focus restored to the trigger) asks before:
  - replacing a ready dataset (the current analysis and conversation will be cleared);
  - replacing documents (the previous collection **will be deleted on the server**);
  - schema confirmation or analysis re-runs that would abandon a conversation with pending SQL
    approvals (listing the count);
  - reset, replacing `window.confirm`.

  Nothing is asked when no dependent state would be lost.
- **D6 [approval]. In-flight marker.** While a mutation runs, sessionStorage holds
  `{ kind, workspaceId, startedAt }`, with no filenames, questions, SQL, or IDs beyond the
  workspace hint already stored there.
  - After a reload, if a marker survives, the recovery banner adds "A *document upload* was in
    progress when the page reloaded. The workbench shows what the server has; if it is missing,
    run it again."
  - The marker is cleared when the operation finishes, on reset, and on sign-out. Storage
    failures are ignored, as with the workspace hint.
- **D7. Cancellation honesty.**
  - Synchronous operations show no Cancel button, because aborting a fetch doesn't stop the
    server. The busy text says the request can take up to two minutes.
  - Jobs show Cancel (`DELETE /jobs/{id}`): the phase moves to `cancelling` until the job reaches
    a terminal status. The UI never claims an immediate stop, matching the API's best-effort
    semantics.
- **D8. Feedback and announcements.** `OperationAnnouncer` is one always-mounted `role="status"`
  `aria-live="polite"` region in the shell. It announces start, progress milestones (no more than
  once per stage), success, failure, cancellation, and interruption.
  - Errors keep `role="alert"` banners. Success text names exactly what was saved; nothing claims
    success before the response.
  - Progress shows determinate values only from `progress_total`; otherwise it shows an
    indeterminate indicator with `aria-busy` on the affected region.
  - Animation is disabled under `prefers-reduced-motion`.
- **D9. Retry semantics.** No automatic retry of any mutation.
  - After a retryable failure, the UI re-fetches the workspace overview, then offers an explicit
    "Try again" that reuses the user's still-held draft (file or question).
  - Approval decisions re-sync messages, as today; the server's 409 guards duplicates.
  - Job retry uses `POST /jobs/{id}/retry` only when `retryable` is true.
- **D10. Stale identifiers.** A 404 on a dataset, collection, or conversation during an operation
  triggers the same server re-sync as reload recovery. Hard-coded "expired" handling is not
  duplicated.

## Do

Slices, each committed separately and each keeping `check_all --full` green:

1. **S1: operation model.** Add `operations/model.ts` (reducer and selectors), `operations/
   useOperations.ts`, and `operations/model.test.ts` covering transitions, lane exclusivity, the
   ID-scoped finish, and illegal transitions as no-ops.
2. **S2: App migration.** Replace `task` with operations in `App.tsx` and pass per-lane busy flags
   to components. Add Vitest guards: rapid double clicks start one question and one report, and a
   running question keeps the composer locked. These also pass on the old code (F1 is latent); the
   overlap cases are covered by S1's reducer tests.
3. **S3: feedback.** Add `OperationAnnouncer` in `WorkbenchShell`, the progress and busy styling,
   and reduced motion. Add Vitest tests for announcements and `aria-busy`.
4. **S4: safe replacement.** Add `components/ConfirmDialog.tsx` with focus and Escape tests, and
   apply D4 and D5 to upload, sheet, schema, analysis, documents, and reset. Cover F2, F3, and F4
   in Vitest, plus a mocked-Playwright replacement journey with axe.
5. **S5: recovery.** Add the D6 marker in `workspace/recovery.ts` and the D9 re-sync-then-retry
   flow. Cover reload during upload and indexing in Vitest (`App.recovery.test.tsx`) and in mocked
   Playwright.
6. **S6: jobs.** Add `operations/useJob.ts` with the client's typed job calls (from the existing
   generated schema). Vitest uses fake timers for backoff, `Retry-After`, terminal stop,
   cancel → `cancelling` → `cancelled`, 404 → `interrupted`, and unmount cleanup. Add a
   mocked-Playwright harness spec.
7. **S7: real stack and docs.** Add scripted real-stack journeys: replacement confirmation leaves
   no stale analysis or conversation, and a reload restores truthful state. Add an "Operation
   state" section to `docs/architecture/workbench-shell.md` and update the axe baselines for the
   dialog and announcer.

## Implemented semantics: S4 safe replacement

- **Server behaviour, verified in code.**
  - Size and type validation and rate limits are refused before anything is written.
  - Chroma builds the new index in a staging collection. Only after every batch succeeds does it
    delete the canonical collection and rename the staged one into its place.
  - pgvector's `replace_chunks` is atomic.
  - The repository swaps the collection record only after indexing has finished.
  - An indexing failure therefore leaves the previous documents intact. A client timeout or network
    error tells the browser nothing: the server may still finish the replacement afterwards.
  - Dataset uploads are additive; the overview returns the latest dataset.
- **After any failed dataset or document replacement**, the client reads
  `GET /workspaces/{id}/overview` and compares resource IDs:
  - unchanged: "Nothing was replaced", and current results stay;
  - changed: the new resource is adopted, its dependents are cleared, and the user is told the server
    finished despite the error;
  - collection gone: the user is told so.
- **If the overview cannot be read**, an error banner says the outcome is unconfirmed. Every change is
  paused (`busy`) until **Check server state** succeeds. Results are never assumed valid.
- **Stale responses.** An epoch counter is bumped on every workspace switch, creation, or reset, and
  any response from an earlier epoch is dropped.
- **Pending approvals.** The confirmation text says that clearing the conversation in the browser
  does not run, approve, or cancel pending SQL; the requests stay unanswered on the server. Reset
  says they are deleted with the workspace.
- **Backend follow-up (recommended, not in UI-2).**
  - An idempotency key on document and tabular uploads would make retries after a timeout safe.
  - A transactional "replace if current collection is X" precondition (`If-Match` on the
    collection) would let the client prove which collection a replacement superseded.
  - The narrow window in `vector_store.replace` between deleting the canonical collection and
    renaming the staged one could leave no index after a crash. Promotion by rename-then-delete
    would close it.

## Do not touch

- API routes, OpenAPI, `schema.d.ts`, backend packages, the evaluation baselines, and the
  investigation feature flag. If the hook needs a client function for `/api/v1/jobs`, add it in
  `api/client.ts` against the existing generated types.
- Investigation proposal, approval, progress, and report UI (UI-6b, UI-6c), and the upload,
  profiling, analysis, and document screens' content and layout (UI-3 to UI-5) beyond busy,
  confirm, and error wiring.
- The drafts policy: no question text, SQL, file names, or rows in browser storage. Tenant scoping
  stays server-side; the client keeps treating every ID as an untrusted hint.
- SQL approval semantics and consent gating in `ConversationPanel`.

## Security and authorization

- No new capabilities or endpoints. Job calls use existing `workspace:read` (get/list) and
  `data:write` (cancel/retry) routes, and foreign jobs remain 404.
- The D6 marker carries only an operation kind and the workspace ID already in the hint, which
  grants nothing.
- Confirmations are a UX guard, not authorization; server checks are unchanged.

## Acceptance (from #132)

| Criterion | Evidence (planned) |
|---|---|
| Concurrent operations cannot overwrite each other's state | S1 reducer tests; S2 F1 regression; S6 independent job records |
| Reloading during a job restores truthful state | S5 marker and reload tests; S6 404 → `interrupted`; S7 real-stack reload journey. The real-stack case covers synchronous operations only (see R1) |
| Replacement asks for confirmation and leaves no stale dependent results | S4 Vitest and mocked Playwright; S7 real-stack replacement journey |
| axe and keyboard checks pass | S4 and S7 axe baselines; dialog keyboard tests; existing shell keyboard specs |
| Vitest, mocked Playwright, and scripted real-stack tests cover the new states | Per slice above; real-stack job coverage per R1 |
| `check_all --full` passes; no evaluation case changes | Every slice |

## Risks and mitigations

- **R1 [approval]. No default job producer.** The scripted real stack can't exercise job states
  without investigations, whose UI is UI-6b. Proposal: job states get Vitest and mocked-Playwright
  coverage in UI-2, and real-stack job coverage lands with UI-6b. The alternative, enabling
  investigation routing in the scripted stack now, pulls UI-6b scope forward.
- **R2. The dialog in jsdom.** jsdom lacks `HTMLDialogElement.showModal`, so tests use a small
  polyfill in the test setup. Playwright covers the real behaviour.
- **R3. Timing-dependent tests.** Use fake timers and injected clocks in Vitest. Playwright uses
  `expect.poll` and route control, never fixed sleeps.
- **R4. Behaviour change in D4.** Users now see the old dataset while a replacement runs. The busy
  label states that it is being replaced.

## Non-goals

- Moving analysis or indexing to background jobs, durable jobs, server-side upload resumption,
  and idempotency keys on more endpoints (all backend work).
- Investigation UI (UI-6b, UI-6c), and screen redesigns (UI-3 to UI-5, UI-7).
- Multiple concurrent mutations.

## Evaluation and docs impact

- No change to `evals/v1/` or its baseline.
- Update `docs/architecture/workbench-shell.md` (operation state, replacement, recovery).
- No ADR: the decisions refine ADR 0003 and 0020 on the client side only.
