# Brief: #154 UI-6a Ask workspace, conversation history, citations, and SQL approval

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/154
Tier: strongest. Check mode: --full.

## Outcome

Asking a question has one clear lifecycle in the browser: sending, then answered, refused, awaiting
approval, not executed, or failed. A failed or unconfirmed submission is checked against the server
before anything can be asked again, so a timeout can no longer turn into a duplicate question. The
history is an ordered, navigable list with server timestamps. Citations that point at nothing are
marked as such. SQL approval explains each server outcome (already decided, cancelled, gone, refused)
instead of showing a raw error. Everything builds on UI-2's operation model, announcer, and stale-epoch
rules, with no API change.

## Baseline and worktree

- Base: `origin/main` at `7556f1b` (PR #199, UI-2, merged), verified on 2026-10-29.
- Worktree: `work/agent-worktrees/154-ui6a-ask-conversations`, branch
  `feat/iteration2-ui6a-ask-conversations`.
- Related: ADR 0003 (React frontend), 0004 (tenancy), 0011 (primary product), 0020 (process-local
  execution), 0024 (multi-agent investigation, for the UI-6b boundary);
  `docs/architecture/workbench-shell.md`, `evidence-grounding.md`, `langgraph-sql-approval.md`.

## Facts (verified against `7556f1b` on 2026-10-29)

**Contract (no gaps assumed; all from `apps/api/feature_routes.py`, `models.py`, `schema.d.ts`)**
- `POST /workspaces/{id}/conversations` and `POST /conversations/{id}/messages` are
  non-idempotent and take no `Idempotency-Key`. The message route runs the whole workflow
  synchronously and persists the message **only on success**. A 422
  `question_could_not_be_answered_safely` stores nothing.
- The message route refuses with 409 `sql_approval_already_pending` while any message is pending,
  and with 409 `gemini_consent_required` without consent. It is rate-limited (429).
- `question` is 1 to 4,000 characters. The composer enforces no limit today.
- `MessageResponse` has `id`, `conversation_id`, `created_at`, `question`, `answer`, `route`
  (`memory | sql | rag | hybrid | unsupported | investigation`), `status`
  (`complete | pending_approval | rejected`), `sql`, `rows | null`, `sources[{citation}]`,
  `provenance`, `request_id`, and an optional `proposal`.
- There is no `failed` or `refused` message status. Out-of-scope questions come back as
  `route: "unsupported"` with `status: "complete"`.
- `GET /conversations/{id}/messages` pages in `(created_at, id)` order. The client already follows
  every page.
- The overview's `conversation_id` is the workspace's latest conversation. There is no endpoint for
  listing conversations.
- **Retention.** `add_message` keeps the last `MAX_CHAT_MESSAGES` (50) messages and the result rows
  of only the last `MAX_CHAT_DATAFRAMES_RETAINED` (10). Older complete SQL answers return
  `rows: null`.
- `POST /messages/{id}/approval` `{decision, sql?}`:
  - 409 `sql_approval_not_pending` once a message has been decided or is no longer pending;
  - 422 `sql_approval_could_not_be_applied_safely`, after which the server has already resolved the
    message to `rejected`;
  - 404 for a foreign, deleted, or expired message;
  - 403 without `analysis:run`.
- Checkpoints are process-local. After a restart a pending message is rewritten to `rejected`
  ("cancelled because the service restarted"). There is no time-based approval expiry.
- **Server race (not fixed here).** Two concurrent decisions on the same message can both pass the
  pending check. The second `resolve_message` then raises an uncaught `ValueError` (500). SQL is
  read-only, so this is a duplicate read, not a write.
- No endpoint serves the original PDF. Citations are strings
  ("Source N - file, page P, chunk C[, relevance R]: excerpt"), with an excerpt of up to 240 redacted
  characters.

**Client today**
- `App.tsx:handleQuestion` creates the conversation lazily, then asks. On any error it shows the
  banner and `ConversationPanel` puts the text back into the composer. Nothing checks whether the
  server stored the message anyway.
- `handleQuestion`, `handleApproval`, and `resyncMessages` don't check `epoch` (UI-2's stale guard).
  Lane exclusivity keeps this latent today.
- `ConversationPanel` has its own `role="status"` region in addition to the shell's
  `OperationAnnouncer`.
- `MessageCard` renders a user/assistant pair per message. Only `pending_approval` and `rejected`
  get a status chip; there are no headings per exchange and no timestamps.
- `MessageCard` turns "Source N" into a link only when that source exists. An unmatched "Source N"
  stays as plain text, indistinguishable from a valid one. The source footer says "Open the PDF page
  to read it in context", but nothing in the app can open it.
- `ApprovalControls` disables every action while any mutation runs. Errors show the server message
  and re-list the messages.
- Coverage:
  - Vitest: `App.*.test.tsx`, `MessageCard`, `AnswerProvenance`, `evidence`, `conversationStatus`.
  - Mocked Playwright: `e2e/*.spec.ts`, with axe in `accessibility.spec.ts`.
  - Scripted real stack: `e2e/real/retail-journey.spec.ts`, which already covers approve, reject,
    edit, restart cancellation, network failure, citations, and phone width.

## Findings (S0)

| ID | Defect or limit | Severity |
|---|---|---|
| A1 | A timeout, network error, or 5xx on `POST /messages` may still have stored an answer. The question goes back into the composer and re-asking duplicates a non-idempotent, model-billed request (or hits 409 if the first one is pending approval) | High |
| A2 | Question and approval handlers and `resyncMessages` don't check the epoch, so a late response could land in a newer workspace. Latent: lanes forbid the overlap today | Medium |
| A3 | Two live regions announce one answer twice ("Question finished." and "Answer ready…") | Medium |
| A4 | A failed question leaves no trace in the thread, only a banner. Out-of-scope (`unsupported`) answers look like ordinary answers. While a question runs, only the button label changes | Medium |
| A5 | The composer allows more than 4,000 characters, so the server answers with a generic 422 | Low |
| H1 | No timestamps, no per-exchange headings, and no list semantics, so screen-reader users cannot move between exchanges | Medium |
| H2 | A complete SQL answer whose rows were evicted (`rows: null`) shows neither table nor explanation, while the SQL and the "computed from the rows below" hint remain | Medium |
| C1 | A "Source N" with no retrieved passage looks like valid text: an invented or invalid citation is not flagged inline | Medium |
| C2 | Citation links are all named "Source N" with no file or page context, and there is no way back from the passage to the answer | Low |
| C3 | "Open the PDF page to read it in context" implies the app can open it | Low |
| P1 | Approval 409, 404, 403, and 422 outcomes show the raw server message. "Already decided", "cancelled by restart", and "gone" are not explained | Medium |
| P2 | After a network error on a decision, the re-sync result is not explained. If the message is still pending, the earlier request may still be running on the server | Medium |

## Design decisions (S0)

Decisions marked **[approval]** change behaviour or scope and need maintainer sign-off before
implementation. The others follow from existing contracts.

**Maintainer approval (2026-10-29).** D2, D3, and D8 are approved as written: reconciliation with
the unconfirmed lock, client-only sending and failed entries, and approval without an extra
confirmation dialog.

- **D1. One lifecycle module.** Add `apps/web/src/conversation/thread.ts`, a pure module with no
  React:
  - `outcomeOf(message)` returns `answered | out_of_scope | awaiting_approval | not_executed`.
  - `threadEntries(messages, local)` merges server messages with at most one client-only entry
    (`sending` or `failed`).
  - `outcomeAnnouncement(...)` produces the announcement text.

  It is the single source of the wording used by chips, announcements, and the context bar.
  `conversationStatus.ts` reuses it.
- **D2 [approval]. Reconcile uncertain submissions; never retry automatically.**
  - On a retryable failure (network error, timeout, 5xx) of `createConversation` or `askQuestion`,
    the client reads `GET /workspaces/{id}/overview`, then `listMessages` for its conversation.
  - **Evidence.** A message that is not on screen, has the same question text, and was created after
    the request started is adopted. The user is told "the server answered despite the error" and
    the draft is not restored.
  - **None.** The draft is restored with "Nothing was saved; you can ask again."
  - **Read failed.** The outcome is *unconfirmed*: the composer stays locked behind
    **Check server state** (UI-2's `unconfirmed` pattern), and nothing is re-sent.
  - Non-retryable errors (4xx) are definite. Only 422, 409, and 429 restore the draft immediately.
- **D3 [approval]. Client-only entries in the thread, memory only.**
  - While a question runs, the thread shows the user's question with a "Sending, can take up to two
    minutes" assistant placeholder (`aria-busy`).
  - On a definite failure it becomes a **Failed, not saved** entry with the error category and
    request ID. It offers **Edit and ask again**, which moves the text to the composer and removes
    the entry.
  - Nothing is written to browser storage (the drafts policy is unchanged). Entries are dropped on
    workspace change, reset, or the next successful question.
- **D4. Stale responses.** Every conversation handler captures `epoch` and the conversation ID and
  drops a response if either changed. A message whose `conversation_id` differs from the active
  conversation is never merged. Rejected responses finish their operation as `cancelled`, the same
  way as `discardStale`.
- **D5. One announcer.** `ConversationPanel`'s live region is removed. Question and approval
  outcomes go through the shell's `OperationAnnouncer` with the D1 wording (for example
  "Answer ready", "SQL approval required. Nothing has run yet.", "Question failed. Nothing was
  saved."), which replaces the generic "Question finished.". Errors keep their `role="alert"`
  banner.
- **D6. History semantics.**
  - The thread is an `<ol aria-label="Conversation history">`. Each exchange is an `<li>` with a
    visually hidden `<h3>` ("Question 3 of 5"), so screen-reader heading navigation moves between
    exchanges.
  - Each exchange shows a `<time dateTime>` from `created_at`, in local time with the date when it
    is not today, and an outcome chip for every outcome.
  - Order is the server's: the client appends only after success, and a re-sync replaces the list.
  - An empty conversation, and a failed history load after recovery, each get honest text.
  - A complete SQL answer with `rows: null` says that the rows are no longer retained (the newest 10
    answers keep them) and hides export.
- **D7. Citation inspection.**
  - A "Source N" with a matching passage becomes a link labelled "Source N: file, page P". Opening it
    reveals and focuses the passage, as today.
  - Each passage has a "Back to the answer" link that returns focus to the citing link.
  - A "Source N" with no passage renders as marked text, "Source N (not among the retrieved
    passages)", in warning style. It is never a link or an invented detail.
  - Unparseable citations are shown raw, as today.
  - The footer wording says the excerpt is all the workbench holds, and that the original PDF is not
    served by the app.
  - No document content beyond what `sources` returns is fetched.
- **D8 [approval]. SQL approval.**
  - The decision panel states the consequence of each choice before the person acts:
    - **Approve:** runs this read-only query on the uploaded table in this workspace; the rows are
      kept with the conversation and sent nowhere else.
    - **Edit:** the edited query is revalidated against the same rules.
    - **Reject:** nothing runs.
  - No extra confirmation dialog: the explicit button is the decision. This keeps the existing
    keyboard flow and real-stack tests. A frontend click is never authority; the server re-checks
    everything.
  - Outcome mapping after a decision error, always followed by re-listing the messages from the
    server:
    - 409 `sql_approval_not_pending`: "This request was already decided or cancelled. Showing its
      current state."
    - 422: "The decision could not be applied safely. Nothing was executed." The message is now
      rejected.
    - 404: the message or workspace is gone, and the UI-2 recovery re-sync runs.
    - 403: "You are not allowed to decide SQL in this workspace."
    - Network, timeout, or 5xx: re-sync. If still pending: "Your decision may not have reached the
      server, or may still be running. Check again before deciding." **Check again** re-lists, and
      the buttons stay disabled until it does. If resolved: the server's outcome is shown.
  - A restart-cancelled message (`rejected` with the restart answer) is shown as "Cancelled by a
    service restart. No query was run."
  - Duplicate decisions are impossible from one tab (lane and disabled controls). Across tabs, the
    server's 409 guards them, and the mapping above explains it.
  - This applies to v1 message approval only. INV-10 SQL-wave approval stays in UI-6b.
- **D9. Composer.**
  - `maxLength` is 4,000, with a counter announced only near the limit.
  - Enter sends and Shift+Enter adds a newline. A visible hint states this.
  - The reason a disabled composer is disabled (no data, consent, pending approval, or another
    operation running) is shown as text tied to it with `aria-describedby`, not only as a
    placeholder.
  - Proposals for UI-6b render unchanged (`message.proposal` stays a read-only card).

## Do (slices, each committed separately with `check_all` green)

1. **S1 thread model.** `conversation/thread.ts` and its tests (D1). `conversationStatus.ts` delegates
   to it.
2. **S2 submission lifecycle.** Epoch and conversation guards (D4), reconciliation and the
   unconfirmed lock (D2), client-only entries (D3), one announcer (D5), and composer limits and
   hints (D9). Vitest in `App.ask.test.tsx`: duplicate prevention, pending, answered, out of scope,
   awaiting approval, definite failure, timeout with server evidence, timeout without evidence,
   unconfirmed lock and check, stale responses after a reset, a single announcement.
3. **S3 history.** List and heading semantics, timestamps, outcome chips, rows-evicted notice, and
   empty and load-failure text (D6). Vitest in `MessageCard` and `ConversationPanel`.
4. **S4 citations.** D7. Vitest: valid, invalid, short, and raw citations; focus there and back;
   accessible names.
5. **S5 approval.** D8. Vitest in `App.approval.test.tsx`: approve, reject, edit, 409, 422, 404,
   403, network then still pending, network then resolved, restart-cancelled wording, duplicate
   clicks.
6. **S6 browser and docs.**
   - Mocked Playwright `e2e/ask-workspace.spec.ts`: keyboard-only ask, citation jump and back,
     approval outcomes, a timeout reconciled, phone width, and axe on each state.
   - Scripted real stack: a timeout on the message POST (route held through to the response) adopts
     the stored answer without a second POST, plus an invalid citation marker if the scripted
     provider can produce one; otherwise it stays Vitest-only and is stated.
   - Docs: an "Ask workspace (UI-6a)" section in `docs/architecture/workbench-shell.md`.

## Do not touch

- API routes, OpenAPI, `schema.d.ts`, backend packages, evaluation baselines, and the investigation
  feature flag or routing.
- Investigation proposal approval, progress, and cancellation (UI-6b); the verified-report and
  investigation-wide evidence inspector (UI-6c); and PROD-1.
- SQL policy and the read-only guards. The server remains the only authority for approval.
- The drafts policy: no question text, SQL, or rows in browser storage. The D2 and D3 state is held
  in memory only.

## Security and authorization

- No new endpoints, capabilities, or storage. Reconciliation uses existing `workspace:read` reads.
  Every ID stays a hint that the server authorizes; foreign resources remain 404.
- Reconciliation adopts only messages returned by the tenant-scoped list for the active
  conversation, never by matching question text across conversations.
- Citation rendering shows only server-returned citation strings. Unmatched references are labelled
  and never resolved.
- Telemetry and audit fields are unchanged.

## Acceptance (from #154)

| Criterion | Evidence (planned) |
|---|---|
| Ask, history, citations, and SQL approval redesigned without regressions | S1 to S5 Vitest, the existing real-stack journeys kept green, mocked Playwright |
| Accessibility and reliability verified with Vitest, mocked Playwright, and scripted real-stack tests | S6, plus axe at desktop and phone width |
| `check_all --full` passes; no evaluation case changes | Every slice; `evals/` untouched |

## Risks

- **R1. Text-match adoption (D2).** Two identical questions asked quickly could match the wrong
  message. Mitigations: only one question can run at a time; the match also requires
  `created_at >= startedAt − 5 s` skew allowance and that the message is not already on screen.
  Messages that are adopted are labelled as such.
- **R2. Real-stack timeout simulation.** Holding a route until after the client's 120 s timeout is
  too slow. The real-stack journey aborts the response (`route.fetch` then `abort`) after the server
  has stored it, which gives the same client view.
- **R3. Backend follow-ups (separate issues, not UI-6a):**
  1. `Idempotency-Key` on `POST /messages` and `POST /conversations`, which would make D2's
     text-match unnecessary.
  2. A compare-and-set in the approval route, so a concurrent second decision gets a 409, not a 500.

## Non-goals

- Streaming answers (the API is synchronous), conversation switching or listing (no endpoint), and
  deleting messages.
- Investigation UI (UI-6b and UI-6c), and backend changes.

## Evaluation and docs impact

- No change to `evals/v1/` or its baseline.
- `docs/architecture/workbench-shell.md` gains an Ask workspace section.
- No ADR: this refines ADR 0003 on the client side only.
