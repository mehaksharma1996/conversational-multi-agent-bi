# React workbench shell

The React client uses a persistent workbench shell around the existing upload, analysis, document,
and conversation workflows. The shell changes presentation and navigation only; FastAPI resources,
security decisions, consent, SQL approval, provenance, recovery, and exports retain their existing
contracts.

## Navigation model

The five primary sections are Overview, Data, Analysis, Documents, and Ask. `App.tsx` owns the
active section because it also owns the workspace resources used by every workflow. The sidebar is
a presentational component and reports navigation intent back to the app.

Sections use native URL fragments (`#overview`, `#data`, `#analysis`, `#documents`, and `#ask`) so
they can be deep-linked and traversed with browser history without adding a router dependency.
Only section changes write history entries. An empty fragment is Overview; a legacy citation
fragment (`#source-…`) opens Ask; any other fragment, such as the skip link's `#main-content`,
leaves the current section unchanged.

Source citations keep their `#source-<message>-<n>` identifiers, but activating one is handled in
the page: it opens the collapsed source list, focuses the cited passage, and neither changes the
URL nor adds a history entry. Back and Forward therefore always step between sections.

On an intentional section change, focus moves to the section heading. The selected link exposes
`aria-current="page"`. Desktop navigation can collapse to its labelled icons.

## Phone navigation drawer

At phone widths (`max-width: 760px`, mirrored in `WorkbenchShell.tsx`) the sidebar becomes a
modal drawer:

- closed, it is `inert` and `visibility: hidden`, so its links are neither focusable nor exposed;
- open, it is a labelled `dialog` with `aria-modal="true"`, the rest of the workbench is `inert`,
  and Tab/Shift+Tab cycle inside it;
- opening focuses the active section link; Escape, the in-drawer close button, and the backdrop
  close it and return focus to the toggle, while choosing a section focuses that section heading;
- the toggle's accessible name states its action ("Open navigation" / "Close navigation");
- leaving the phone layout closes the drawer, and entering it moves focus out of the sidebar to
  the toggle before the sidebar becomes inert.

## Drafts across sections

Only the active section is mounted, so unsaved work is owned by `App` through
`src/workspace/drafts.ts`: the selected dataset file and workbook sheet, selected PDFs, schema
mapping edits, the question and approval option, and in-progress SQL approval edits. Drafts are
memory-only (never browser storage) and end with the App, which the auth gate unmounts on
sign-out. Schema edits are keyed to their dataset id and approval edits to their pending message;
approval edits are dropped when the conversation is replaced or the decision is made, and every
draft is cleared by workspace reset.

Browsers cannot restore a file input's selection, so a remounted upload control names the kept
file(s) beside the input with a "Clear selection" action; choosing a file replaces the selection.

## Operation state, replacement, and recovery (UI-2, #132)

Design record: `docs/agents/briefs/132-ui2-operation-state-recovery.md`.

- **Operations.** `src/operations/` gives every operation its own record (`queued`, `running`,
  `cancelling`, then `succeeded`, `failed`, `cancelled`, or `interrupted`), so one finishing never
  clears another's busy state.
  - Two lanes: `mutation` (anything that changes workspace resources; at most one, matching the
    non-idempotent API) and `read` (report and result downloads, and workspace export).
  - A second request in a busy lane is refused, not queued.
  - The workbench still disables every action while any operation runs, so lanes do not overlap
    in today's UI. The model is what lets UI-6b's jobs run beside other work.
- **Feedback.** One always-mounted polite live region (`OperationAnnouncer`) announces start,
  finish, failure, cancellation, and interruption.
  - Slow synchronous requests say they can take up to two minutes, because aborting a fetch would
    not stop the server; synchronous requests have no Cancel button.
  - The active section is `aria-busy` while a mutation runs. Errors stay `role="alert"` banners.
- **Confirmation.** `ConfirmDialog` is a native `<dialog>` opened with `showModal()`, so the rest of
  the page is inert.
  - Cancel is focused first, Escape cancels, and focus returns to the opener, or to the section
    heading if the opener is gone.
  - It asks before replacing a dataset or documents, before a schema confirmation or analysis
    re-run abandons pending SQL approvals, and before reset.
  - Each dialog lists the consequences, including that the server deletes replaced documents and
    that clearing the conversation does not run, approve, or cancel pending SQL on the server.
- **Replacement.** Results stay on screen until a replacement succeeds. After a failure the client
  reads the workspace overview and compares resource IDs, because a timed-out request may still
  have completed on the server. If the overview cannot be read, all changes pause until
  **Check server state** succeeds. A workspace epoch drops responses that belong to an earlier
  workspace. This is client-side consistency, not a server transaction (see the brief's backend
  follow-ups).
- **Reload.** While a workspace-changing request runs, sessionStorage holds only `{kind,
  workspaceId, startedAt}`. After a reload the marker is compared with the server's `created_at`
  times:
  - the operation "appears to have completed" when a matching resource is newer;
  - otherwise its outcome "cannot be confirmed";
  - a marker for any other workspace is never applied to the restored one.

  Nothing is retried. Clock skew between browser and server can misclassify an operation, so the
  wording stays tentative and shows both times.
- **Jobs.** `useJob` polls `/api/v1/jobs/{id}` one request at a time with 1–10 s exponential
  backoff, and always stops: at a terminal status, on 404, on 401/403, after five transient
  failures, or after 200 reads.
  - Jobs are process-local, so after an API restart a 404 means the outcome is unknown, never
    success.
  - Cancellation reports requested, acknowledged, completed, or too late, and never claims an
    immediate stop.
  - `JobProgress` renders the linked investigation job status (UI-6b).

## Ask workspace (UI-6a, #154)

Design record: `docs/agents/briefs/154-ui6a-ask-conversations.md`.

- **Lifecycle.** `src/conversation/thread.ts` holds the one outcome vocabulary: answered, out of
  scope (`unsupported` route), investigation proposed, awaiting approval, not executed, and
  cancelled by restart. Chips, exchange headings, and announcements all use it.
  - The server stores a message only when a question succeeds. The thread therefore adds at most one
    memory-only entry: **Sending** while the request runs, or **Failed, not saved** after a definite
    failure, with the text back in the composer. Nothing about a question is written to browser
    storage.
- **No duplicate questions.** Messages are not idempotent.
  - After a lost connection, a timeout, or a 5xx on a question that was sent, the client keeps the
    question operation open and re-reads the conversation.
  - **Answer found.** A new message with the same text, created after the request started, is
    adopted and the question is not sent again.
  - **Nothing found.** The question is reported as not saved.
  - **Read failed.** Asking pauses behind **Check server state**.
  - A failed conversation start is definite, because nothing was asked.
- **Stale responses.** Question and approval handlers drop a response from an earlier workspace
  epoch, or one for another conversation or message.
- **History.** The history is an ordered list in server order. Each exchange has a visually hidden
  `h3` ("Question 2 of 5") for heading navigation, an outcome chip, and the server's `created_at`
  as a `<time>`. A complete SQL answer whose rows the server evicted (only the newest answers keep
  rows) says so and offers no export.
- **Citations.**
  - A "Source N" with a retrieved passage is a link named with the file and page. It opens and
    focuses the passage, which links back to the first citing link.
  - A "Source N" without a passage is marked "(not among the retrieved passages)" and is never
    linked.
  - Only server-returned citation strings are shown. The original PDF is not served.
- **SQL approval.**
  - The decision group describes each choice's consequence.
  - After a failure the client re-reads the messages and explains the server's answer: already
    decided or cancelled (409), applied safely with nothing executed (422), not allowed (403), or
    gone (404).
  - After a lost connection, a decision the server applied is shown. One still pending keeps the
    controls disabled until **Check again** reads the server, because the first request may still be
    running.
  - The click is never authority: the server re-checks the request, the policy, and the read-only
    guards.
  - This covers v1 message approval only; investigation SQL waves have their own review (below).
- **Announcements.** The panel has no live region of its own. Question and decision outcomes go
  once through `OperationAnnouncer` in the outcome's own words, replacing the generic "finished".

## Investigation workflow (UI-6b, #155)

Design record: `docs/agents/briefs/155-ui6b-investigation-workflow.md`. API read contract:
`docs/architecture/investigation-workflow.md` ("UI-6b read contract").

**Availability.** Production investigation execution is unavailable until PROD-1 (#198): with no
reviewed bounded adapter, planning fails closed as `failed / metering_unbounded`, and the UI shows
the server's explanation. Investigation routing is off by default
(`INVESTIGATION_ROUTING_ENABLED`). The scripted adapters used by the browser journeys prove wiring,
not production model support.

- **Scope.** `InvestigationPanel` attaches to the latest INV-9 proposal message in Ask. It shows,
  in order: an explicit **Start investigation** (planning only), planning, the full plan review,
  execution with cancellation, the SQL-wave review, and terminal status. It does not render the
  verified report or evidence; that is UI-6c (#156).
- **Controller.** `src/investigations/useInvestigation.ts` owns one in-memory idempotency key, the
  latest authoritative response, and one-at-a-time bounded polling (1–10 s, as UI-2). It never
  retries a POST.
  - Discovery and recovery use only the read-only by-message lookup.
  - A generation counter plus the workspace, conversation, proposal, investigation, and job IDs
    drop late responses.
  - Commands use a short-lived `investigation` operation in the mutation lane. The background job
    does not hold the lane.
- **Plan review.** Objective, sub-questions, dependency-ordered tasks, hypotheses, missing
  information, approval requirements, the planning-phase cost estimate worded by its bound, and
  the server deadline. Approval is refused in the browser when trusted review details or task
  details are missing. Approve, reject, and cancel use `ConfirmDialog`.
- **Progress.** Only server-recorded task status and approved-task-owner counts ("task-owner
  progress", not "all agents"). A native `<progress>` appears only with a real total. Absent data
  is stated as unavailable; nothing is simulated. A missing process-local job is an unknown job
  outcome beside the durable lifecycle record.
- **Cancellation.** Uses the investigation endpoint and always says an in-flight provider or SQL
  call may finish. The UI distinguishes requested, server-confirmed, and too late.
- **SQL waves.** Statements come only from the authorized SQL-wave endpoint and must match the
  durable wave summary exactly. Every item needs approve, reject, or a material edit, and nothing
  is sent before **Submit decisions**. The submission echoes only the server's wave and item
  bindings, and edits are revalidated by the server. SQL lives only in React memory. It is cleared
  on submission, cancellation, workspace or scope change, an unconfirmed outcome, or a lifecycle
  change, and it is not fetched again while a command is in flight. The server decides expiry: a
  409 is reconciled by an authoritative read, never by the browser clock.
- **Uncertain outcomes.** A retryable failure triggers one read-only reconciliation. If that read
  does not prove a transition, every consequential control stays locked behind **Check server
  state**.
- **Restart.** The durable record reads `interrupted` with its fixed explanation. Nothing resumes,
  and no resume action is offered.

## Workspace context

The context bar reports only state already held by the client:

- workspace availability;
- the dataset filename or schema-review state;
- indexed-document count;
- analysis availability/completion; and
- conversation question count, with any SQL approval still awaiting a decision.

The conversation status never counts a pending approval or a rejected request as an answer; the
Overview names answered, awaiting-approval, and not-executed questions separately.

Export and reset remain persistent workspace actions at every width; at phone widths export keeps
its full accessible name with a shortened visible label and stays ahead of reset. Overview provides compact entry points into
the same workflows and never marks a step complete unless the corresponding resource exists.

## Visual system

`apps/web/src/styles.css` defines semantic surface, text, border, action, success, warning, and
danger tokens plus spacing, radius, focus, and responsive rules. The shell favors flat surfaces,
compact controls, restrained borders, and a high-information layout. It contains no decorative
motion, gradients, glass effects, or new runtime dependencies. Reduced-motion and forced-colors
preferences receive explicit fallbacks.

## Responsive boundary

At wide widths, navigation and context remain visible alongside the active workflow. At tablet
widths, context summaries wrap while workflow content retains its usable width. At phone widths,
the sidebar becomes a drawer and context metadata is progressively condensed, while both workspace
actions remain available; the active workflow remains the only mounted primary section. Wide result tables and SQL blocks retain their own
horizontal scrolling rather than widening the page.

Detailed redesign of the individual workflows is intentionally deferred to later stages.
