# Brief: #155 UI-6b investigation proposal, approval, progress, and cancellation

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/155
Tier: strongest. Check mode: --full.

## Outcome

The Ask workspace turns an INV-9 `investigation_proposed` message into an explicit, reviewable
INV-11 investigation. It shows the server-validated plan and honestly worded planning-cost
estimate, lets an authorized analyst approve or reject the exact plan, follows bounded background
jobs and actual task-owner progress, handles SQL-wave decisions through the investigation API,
requests cancellation without claiming immediate interruption, and recovers authoritative state
after uncertain requests, reloads, workspace changes, and API restarts.

UI-6b does not render the verified report or its evidence. Production model execution remains
unavailable until PROD-1 (#198), investigation routing stays off by default, and every decision is
rechecked by the server.

## S0 contract inventory (verified against `bf7c623` on 2026-10-30)

### Existing backend

- INV-9 adds `MessageResponse.proposal`, a read-only `investigation_proposed` object with status
  `not_started`, the source message reference, fixed reason codes, and reviewed summary. It grants
  no authority and contains no plan or execution handle.
- `POST /api/v1/conversations/{conversation_id}/investigations` requires `analysis:run`,
  `{message_id}`, and `Idempotency-Key`; it returns 202. Creation is durable and idempotent by
  tenant/key hash, accepts only a reconstructable proposal message from that conversation, and
  refuses a second live investigation for the conversation.
- `GET /api/v1/investigations/{id}` requires `workspace:read`; foreign and unknown IDs are the same
  404. Its current response contains lifecycle status, plan/digest/epoch, pending SQL-wave identity,
  fixed terminal explanation, and report.
- Plan decisions require `analysis:run` and echo the exact plan digest and authority epoch. The
  browser supplies no approver. Rejection is terminal; approval submits an execution job.
- `GET /api/v1/investigations/{id}/sql-wave` requires `analysis:run` and is the only endpoint that
  returns the pending statements. Its response includes the exact wave ID/digest, item
  fingerprints, policy status, and deadline. `POST` supports approve, reject, and server-revalidated
  edits and resumes through a new job.
- `POST /api/v1/investigations/{id}/cancel` requires `analysis:run` and returns 202 with the current
  record. Calls already in flight finish and their results are discarded; the response is not proof
  of terminal cancellation.
- Planning, execution, and resume use the process-local bounded job executor. Job status is
  tenant-scoped and content-free. API startup changes every durable non-terminal investigation to
  `interrupted`; completed records and reports remain.
- Production adapters fail closed as `failed / metering_unbounded`. Scripted adapters are injected
  only in tests. The feature flag remains off by default.

### Existing frontend

- UI-6a renders the INV-9 proposal as a read-only card and preserves ordinary conversation,
  citation, and v1 SQL-approval behaviour.
- UI-2 has per-operation state, stale-workspace epochs, one `OperationAnnouncer`, bounded `useJob`
  polling, truthful missing-job and cancellation wording, and `JobProgress`.
- `apps/web/src/api/schema.d.ts` includes the INV-11 endpoints, but `api/client.ts` has no exported
  investigation types or functions.
- Question, approval, and SQL drafts are memory-only. The only browser-persisted operation marker is
  content-free session storage; it is never treated as server authority.

## Blocking contract gaps

1. **Proposal detail is discarded.** `InvestigationProposal` has `cost_estimate`,
   `missing_information`, and derived `approval_requirements`, but INV-11 stores and returns only
   `proposal.plan`. The UI cannot satisfy cost-estimate acceptance or explain the proposal wrapper.
2. **Plan expiry is not readable.** `PendingPlanApproval.expires_at` exists only inside the approval
   service. `InvestigationResponse` omits it, and a status read does not call `expire_due()` and
   mirror the resulting terminal status. The UI cannot display or poll expiry accurately.
3. **Jobs are not linked.** `_Live.job_id` is set for plan, execute, and resume jobs, but no
   investigation response exposes it. The client cannot use UI-2's authorized `useJob` hook.
4. **Detailed progress is not exposed.** Investigation workers never call `JobContext.progress`.
   The guarded execution state already has actual per-task status/attempts and the approved plan's
   assignee, but the API returns neither. Simulated percentages or agent activity would be false.
5. **Creation and reload cannot be reconciled read-only.** A lost create response leaves the browser
   without an investigation ID, and no conversation/message lookup exists. Replaying POST could
   create work if the first request never arrived, so it is not a safe read-only check. A reload
   likewise cannot rediscover an existing investigation from its proposal message.

These are additive API gaps directly required by #155. They are not UI-6c report/evidence work.

## Design decisions

**Maintainer approval (2026-10-30).** D1 through D6 are approved as written, with four mandatory
conditions: proposal details never recreate authority after restart; progress is derived only from
validated execution state and otherwise null; every uncertain mutation remains locked behind
read-only reconciliation with no automatic POST retry; and SQL-wave statements remain temporary,
authorized UI state that is cleared on decision, workspace change, authority loss, or unmount.

### D1. Minimal additive investigation read contract [approval]

Extend `InvestigationResponse` without changing existing fields:

- `proposal`: a typed, nullable review object containing the validated plan's planning-phase
  `CostEstimate`, missing-information strings, derived approval requirements, and
  `plan_expires_at`. It is returned only for the live proposal that the trusted runtime registered.
- `job_id`: the current plan/execute/resume job ID, or null. It is an opaque hint; job endpoints
  still tenant-authorize every read. A restart returns null and the durable investigation says
  `interrupted`.
- `progress`: a typed, nullable content-free snapshot. Each task has ID, kind, approved assignee,
  status, and attempts. Agent rows aggregate only those actual task records by approved assignee.
  No prompt, question, objective, SQL, row, passage, evidence, claim, or report content is added.

The runtime retains the already validated `InvestigationProposal` beside its proposal run and
derives progress from `InvestigationExecutionRun.state()`. If the guarded state cannot be read or is
not initialized, progress is null; it never guesses. Supervisor, Business Investigator, and Critic
activity is not labelled live unless a future backend contract records it. The UI says “task-owner
progress,” not “all agents,” for that reason.

Before every investigation read, the runtime expires due approvals and mirrors the approval
service's terminal status into the durable record. `plan_expires_at` is the registered proposal
deadline, not a browser clock decision. The server remains authoritative at equality and on
workspace expiry.

### D2. Read-only recovery lookup [approval]

Add
`GET /api/v1/conversations/{conversation_id}/investigations/by-message/{message_id}` with
`workspace:read`. It returns the existing investigation for that exact tenant-owned conversation
and source message, or the same 404 used for unknown/foreign resources. It never creates, retries,
approves, resumes, or cancels work.

This is used after an uncertain create response and when restoring a conversation containing a
proposal. The client never automatically repeats creation. Once a read confirms 404, the next
explicit **Start investigation** remains a user-authorized POST with the same in-memory idempotency
key for that attempt.

### D3. One client investigation controller [approval]

Add `apps/web/src/investigations/`:

- `model.ts`: exact lifecycle groups, terminal/active/decision predicates, cost wording, progress
  labels, and decision/cancellation reconciliation outcomes;
- `useInvestigation.ts`: one controller keyed by workspace, conversation, and proposal message. It
  owns the in-memory idempotency key, current authoritative response, bounded status polling,
  unconfirmed-command locks, and UI-2 `useJob` view. A generation plus all returned IDs rejects stale
  responses after workspace, conversation, proposal, investigation, or job changes;
- tests use fake timers and generated API types.

Polling is one request at a time, uses UI-2's 1-to-10-second bounded backoff and stop conditions,
and stops at a decision state, terminal state, authorization denial, repeated transient failure, or
the poll cap. `useJob` follows only the server-returned current job ID. A missing process-local job
is an unknown job outcome; the controller then reads the durable investigation and never fabricates
success.

Commands use a short-lived `investigation` operation in the existing mutation lane so duplicate
clicks are refused. The background job itself does not occupy that lane, so ordinary work can
continue. Investigation transitions and command outcomes use the one `OperationAnnouncer`;
`JobProgress` and the investigation card are not separate live regions.

### D4. Proposal and plan component architecture [approval]

`MessageCard` keeps the INV-9 recommendation. For the proposal associated with the controller,
`InvestigationPanel` progressively renders:

1. **Not started:** the deterministic recommendation, “no execution authorized,” and explicit
   **Start investigation**.
2. **Planning:** server lifecycle plus the linked job state; no invented plan skeleton.
3. **Awaiting plan approval:** server plan objective, sub-questions, dependency-ordered tasks,
   hypotheses when schema v2, missing information, approval requirements, planning cost estimate,
   and server expiry. The exact digest/epoch are held for the decision but not promoted as user
   content.
4. **Execution:** lifecycle, job state, actual task rows, task-owner aggregation, and cancel action.
5. **Awaiting SQL approval:** a separate authorized fetch renders the exact wave. SQL stays only in
   React memory and DOM, is cleared on decision/workspace change/unmount, and is never logged,
   persisted, announced, or sent to telemetry.
6. **Terminal:** accurate status and server explanation. Completed reports link to the future UI-6c
   work only; their content is not rendered here.

Ordinary messages and v1 message-level SQL approval keep their current components and authority.

### D5. Cost wording [approval]

`CostEstimate` is explicitly the **planning-phase estimate** supplied with the proposal:

- `enforced_upper_bound`: “Estimated planning cost: up to $X under the current enforced provider
  limits.” It remains labelled an estimate.
- `indicative`: “Indicative planning-cost estimate: $X. This is not a maximum.” Fixed limitation
  codes are translated into short explanations.
- `unavailable`: “A planning-cost estimate is unavailable,” followed by the fixed reason.

No wording describes an indicative amount as guaranteed, implies it covers execution, or treats a
cost estimate as authorization.

### D6. Consequential actions and uncertain outcomes [approval]

- Plan approval and cancellation use the existing accessible confirmation dialog. Rejection uses
  an explicit terminal-action confirmation in the same dialog. SQL-wave submission uses a visible
  review summary and a final **Submit decisions** button; no statement executes before that submit.
- While a command is in flight its controls are disabled. A same-tab duplicate cannot enter the
  operation lane; server 409s protect other tabs.
- A retryable plan, wave, or cancellation failure triggers a read-only investigation reconciliation.
  If the server proves the transition, show it. If it still shows the old decision state or the read
  fails, mark the decision **unconfirmed**, keep controls locked, and require **Check server state**.
  Nothing is automatically approved, rejected, cancelled, retried, or resumed.
- 403 explains lost permission; 404 explains that the workspace/resource is unavailable without
  revealing tenant existence; 409 re-reads authoritative state; 422 states that nothing was
  accepted safely. Workspace epoch changes discard every result and clear SQL.

## UI state model

| Client state | Authoritative evidence | Available action |
|---|---|---|
| `available` | INV-9 proposal, lookup 404 | Start explicitly |
| `creating` | POST in flight | None |
| `create_unconfirmed` | retryable POST failure, no conclusive read | Check server state |
| `planning` | investigation + optional job | Cancel |
| `awaiting_plan_approval` | plan, proposal review, digest/epoch/expiry | Approve, reject, cancel |
| `queued` / `running` | investigation, job, optional real progress | Cancel |
| `awaiting_sql_approval` | investigation plus authorized wave review | Decide wave or cancel |
| `decision_unconfirmed` | command may have reached server | Check server state |
| terminal | server terminal status/explanation | Start only from a later proposal |
| `unavailable` | 401/403/404 or bounded polling failure | Sign in, request access, reload/check |

The client never moves lifecycle status optimistically. Button-local busy state is not server state.

## Authorization and security boundaries

- The authenticated HttpOnly-cookie session supplies tenant and subject; the browser never sends an
  approver identity, capability, authority, consent, provider, scope, timestamps, or trusted status.
- Plan decisions echo only the exact server plan digest/epoch. SQL decisions echo only the current
  wave ID/digest and each item ID/fingerprint. Every edit goes through server revalidation.
- Proposal and plan text are untrusted display data. React escapes it; it is never interpreted as
  markup, an instruction, a resource ID, or an action.
- Investigation IDs and job IDs are hints, not bearer authority. Every API read is tenant-scoped;
  cross-tenant and unknown resources remain indistinguishable 404s.
- SQL, plan text, questions, rows, passages, and reports are excluded from browser storage, logs,
  telemetry, operation markers, and announcements. No new telemetry or audit attributes are needed.

## Progress, cancellation, and recovery behaviour

- Lifecycle status is always shown. Determinate task progress appears only from the guarded state
  snapshot. Agent aggregation counts the approved assignees' actual tasks; absent data is stated as
  unavailable, never animated or estimated.
- Job progress shows only fields the job API reports. No per-task percentage is synthesized from
  time or polling count.
- **Cancel investigation** sends the investigation cancellation endpoint, not the generic job
  endpoint, so approval state and paused runs are also cancelled. The immediate 202 is “cancellation
  requested” unless the returned investigation is terminal. The UI always states that an in-flight
  provider or SQL call may finish and its result will be discarded.
- After API restart, a status read shows durable `interrupted` plus the fixed explanation. The UI
  says nothing resumes automatically and offers no resume action. A new run requires a later
  explicit proposal/start path.
- Recovery lookup reattaches by the tenant-owned conversation and source message. A session-storage
  investigation ID is unnecessary and is not added.

## Accessibility and responsive behaviour

- Proposal, plan, task, and wave sections use headings, lists, `<dl>`, `<time>`, and native
  `<progress>` only when a real total exists. Status text remains available without colour.
- All controls are native buttons/inputs with visible consequences and disabled/busy reasons.
  Dialogs use the existing focus trap and return-focus behaviour.
- Focus moves to the plan review heading when planning finishes, the SQL-wave heading when a wave
  appears, and the investigation status heading after a terminal command. Poll updates do not steal
  focus.
- The single `OperationAnnouncer` announces meaningful transitions once. It never announces SQL or
  plan text. Repeated polls with the same status are silent.
- Cards stack at phone width; task and wave tables use labelled card rows or bounded horizontal
  scrolling. Existing reduced-motion and forced-colours rules apply; no progress animation is added.

## Testing strategy

### Deterministic API and unit tests

- Proposal review returns cost/bound, requirements, expiry, and no authority-bearing fields.
- Expiry is mirrored on GET; equality with the deadline is expired and no execution starts.
- Job IDs are tenant-scoped hints; restart clears the live link and returns `interrupted`.
- Progress is derived only from validated execution state, contains no content, and groups actual
  task assignees. Unreadable/absent state returns null.
- Recovery lookup covers owned, foreign, unknown, stale message, and multiple historical records.
- Existing creation idempotency, cross-tenant denial, cancellation, SQL-wave binding/edit/expiry,
  and v1 regression tests remain green.

### Vitest

- Proposal rendering and all three cost-bound wordings.
- Creation key reuse, duplicate-click refusal, uncertain creation lookup, and stale responses after
  workspace/proposal changes.
- Plan approval/rejection/expiry, authorization changes, 4xx mapping, and network reconciliation.
- Lifecycle and task-owner progress, missing job after restart, terminal/interrupted states, bounded
  polling, and stale poll rejection.
- Cancellation requested versus confirmed/too late/in-flight caveat.
- SQL-wave authorized fetch, exact binding, per-item approve/reject/edit, expiry, stale wave,
  unconfirmed decision, and SQL clearing.
- Keyboard order, dialog focus, transition focus, one live announcement, and accessible names.
- Existing UI-6a ordinary conversation and v1 SQL approval suites remain unchanged.

### Browser and real stack

- Mocked Playwright: desktop and phone proposal-to-plan flow, keyboard-only approve/reject/cancel,
  task progress, SQL wave, restart interruption, stale workspace, and axe on every decision state.
- Scripted real-stack journeys use injected bounded adapters for creation, plan approval, SQL wave,
  cancellation, and terminal status. They prove API wiring, not production provider availability.
- Production-config journey proves `metering_unbounded` is shown accurately and makes no model call.
- Docker journeys run only when Docker is available and are reported separately from mocked tests.

## Implementation slices (after D1-D6 approval)

1. **S1 API review/recovery contract.** Typed proposal review, expiry synchronization, job ID,
   content-free progress, by-message lookup, OpenAPI regeneration, focused Python tests, ADR/workflow
   docs. One commit.
2. **S2 client model and controller.** Generated client functions, lifecycle/cost pure model,
   bounded controller polling, UI-2 job integration, reconciliation and stale guards. One commit.
3. **S3 proposal and plan UI.** Start, plan inspection, cost wording, approval/rejection/expiry,
   dialogs, focus and responsive styles. One commit.
4. **S4 progress, cancellation, and recovery UI.** Task-owner progress, job/lifecycle states,
   cancellation caveat, missing-job and interrupted recovery. One commit.
5. **S5 SQL-wave UI.** Authorized statement review, exact decision binding, edits, expiry and
   uncertain-outcome lock; memory-only SQL. One commit.
6. **S6 browser coverage and docs.** Mocked Playwright/axe, scripted real-stack journeys, final
   workbench documentation. One commit.

Every slice gets focused checks before commit. Final verification is
`python -m scripts.check_all --full`, `git diff --check`, frontend lint/typecheck/Vitest/build,
mocked Playwright and axe, scripted real-stack tests, OpenAPI compatibility, evaluations, and
Docker-dependent checks when Docker is available.

## Acceptance mapping

| Issue criterion | Evidence |
|---|---|
| Plan and cost estimate use `CostEstimate.bound`; approve/reject/expiry work | S1, S3 API/Vitest/Playwright |
| Live progress per task and agent, truthful cancellation, interrupted explanation | S1, S2, S4 API/Vitest/real stack |
| Accessibility and reliability | S3-S6 keyboard, focus, announcement, axe, stale/reconciliation tests |
| Existing v1 and security guarantees | Existing UI-6a/API suites plus full check and evaluation baseline |

## Non-goals

- UI-6c report rendering, evidence inspection, report download, or evidence payload changes.
- PROD-1 real-provider admission, default feature activation, durable checkpoints, parallel agents,
  or automatic restart/resume.
- A second approval service, client-created authority, v1 message SQL approval reuse, simulated
  progress, browser-persisted SQL/plan/question content, or new telemetry carrying user content.

## Risks and limitations

- Reading the in-memory execution checkpoint concurrently must fail closed to `progress: null`; it
  must not affect execution. Focused concurrency tests are required.
- Task-owner aggregation does not claim live Supervisor/Critic/Business Investigator phases. The
  backend has no such phase contract; adding one belongs to a separately reviewed workflow change.
- A by-message 404 after an uncertain create may race a request still reaching the server. The UI
  therefore keeps the unconfirmed lock until an explicit later check; it never auto-creates.
- Scripted adapters validate the complete workflow but do not establish production model support.
  The final report must state this and #198 explicitly.
