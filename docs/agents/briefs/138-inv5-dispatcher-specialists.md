# S0 implementation brief: #138 INV-5 sequential dispatcher, SQL Analyst, and Document Research

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/138
(part of umbrella #131; depends on #137 INV-4 and #135 INV-2).
Tier: strongest. Check mode: `python -m scripts.check_all` (no API or web change, so not `--full`).

## S0 outcome

INV-5 turns an **approved** investigation plan into sequential, gateway-mediated execution. A
trusted run owner seeds an execution thread from a verified proposal handoff and a trusted
authority. A ledger-bound dispatcher runs one gateway operation per guarded LangGraph step. The SQL
Analyst and Document Research specialists choose only the next typed request. Typed, process-local
evidence payloads are validated against their state records, and a deterministic interim report is
composed without a model call. Everything is exercised with scripted bounded adapters; production
stays fail-closed (`metering_unbounded`) until a reviewed bounded adapter exists.

S0 changes this brief only. It implements no contract, gateway, specialist, graph, or test change.

## Baseline (verified 2026-10-19)

- Base: `origin/main` at `7c7d8d541b97fc9b8526d700dab2c9089da7bc50`, the merge commit of INV-4
  PR #166.
- Branch `feat/iteration2-inv5-dispatcher-specialists`, worktree
  `work/agent-worktrees/138-inv5-dispatcher-specialists`.
- The test suite was not run during S0. S1 starts by recording `python -m scripts.check_all` on
  this base before any change.

## Facts (verified against the code on 2026-10-19)

- `packages/investigation/state.py`:
  - `owned_update` validates one writer's update against the prior state. `_task_move_allowed`
    allows `pending → running` (attempt 1), `pending → skipped|refused|cancelled` (attempts 0),
    `running → running` (+1 attempt), and `running → terminal` (same attempts). A single update
    can therefore never move a task from `pending` to `succeeded`.
  - Only `running` permits task starts, evidence, `succeeded`/`failed` decisions, and spending.
    Every other status allows bookkeeping only.
  - Budget usage must equal the bound `BudgetLedger` snapshot, and the ledger must be closed after
    a terminal status (`_settlement_violation`).
  - `EvidenceRecord.kind` is `query | document`. There is no report channel. `GatewayTool` already
    reserves an unused `"finish_attempt"`.
  - `CHANNEL_OWNERS`: tasks, evidence, decisions, and `budget_usage` are owned by the dispatcher;
    status by lifecycle; plan and follow-ups by the supervisor; `plan_approval` by approval; the
    terminal record by terminal.
- `src/orchestration/tool_gateway.py`:
  - There are five port operations (`begin_attempt`, `research_documents`, `prepare_sql`,
    `execute_sql`, `prepare_correction`), exposed through `SpecialistGateway`.
  - Each call refuses a stale state. If `len(view.decisions)` is not the count the gateway last
    issued, it closes with `state_invalid`, so every issued update must be applied before the next
    call.
  - `_close` is private. There is no owner close for completion, cancellation, or shutdown.
  - `research_documents` with no collection, or with a resolver returning `None`, ends the task
    `refused/resource_unavailable` after an attempt has been charged.
  - The executor's `QueryTimeoutError` ends the task `failed/timeout`.
  - Payloads (`SQLEvidencePayload` holds the DataFrame and SQL; `DocumentResearchOutcome`) are
    raw and stored only on success through `evidence_payload(id)`.
- `packages/investigation/refusals.py`:
  - `request` severity leaves the gateway open.
  - `task` severity (`resource_absent`, `sql_approval_required`, `correction_limit`) ends the task.
  - Every other code is `investigation` severity: it closes the gateway and ledger, with a target
    of `cancelled`, `budget_exhausted`, or `failed`.
- `packages/investigation/lifecycle.py`: `running → completed | completed_partial |
  budget_exhausted | awaiting_sql_approval | cancelled | failed | interrupted`. `expired` is not
  reachable from `running`.
- `src/orchestration/investigation_graph.py`:
  - Execution mode binds the dispatcher writer to `gateway.ledger` and refuses a ledger-bound
    Supervisor writer.
  - Every compile creates a fresh strict `InMemorySaver`.
- `src/orchestration/investigation_proposal.py`: the proposal thread ends in
  `awaiting_plan_approval` with a write-once `proposal_record` holding the plan digest. Nothing
  approves or executes it.
- `packages/investigation/validation.py`: `validate_plan` rejects `evidence_refs` to unknown
  evidence, and `TASK_PROPOSERS` lets the Supervisor propose only `sql_analysis` and
  `document_research`. An INV-5 plan therefore has no hypothesis tests and no evidence-citing
  task.
- `src/storage/query_executor.py`: `max_rows=500` and `timeout_seconds=5.0` by default.
  `GuardedSQLService.execute` returns `succeeded | refused | retryable | exhausted`, with one
  correction (`MAX_SQL_CORRECTIONS = 1`).
- `src/llm/bounded.py`: no production client is a `BoundedModelAdapter`.
- `tests/test_subscription_churn_cases.py` already indexes the four churn PDFs with a deterministic
  term embedder. `evals/investigation/v1/reference.sql` and `oracle.json` are evaluator-only.

## Approved decisions (R1–R8, approved 2026-10-19; do not reopen)

| ID | Decision |
|---|---|
| R1 | **Re-seed, not shared saver.** Execution runs on a new strict thread (`<tenant>:<workspace>:<investigation>:execution`), seeded from a verified `ApprovedPlanHandoff`. The builder and saver are unchanged. |
| R2 | A normal end with **zero** succeeded tasks is `failed` (category of the first non-succeeded task in plan order), never an empty `completed_partial`. |
| R3 | **Derived evidence is contract-only in INV-5.** `DerivedEvidencePayload` and its validator are defined, but there is no state change and no producer; production starts in INV-6. |
| R4 | **Document-derived policy criteria are deferred** to the milestone where tasks first cite evidence (INV-6/INV-7). The ADR 0024 clarification is S6 documentation work. ADR 0024 is not edited before S6. |
| R5 | **In-run skips and task closes go through the gateway** (`skip_task`, `finish_attempt`). After the gateway has closed, the dispatcher writes only state-validated closing bookkeeping. |
| R6 | **Retry eligibility** is decided by the dispatcher policy (see "Retry eligibility"), never by a specialist. |
| R7 | **Placement.** Pure dispatch policy lives in `packages/investigation/dispatch.py`. The ledger-bound dispatcher node and the run owner live in trusted `src/orchestration/investigation_execution.py`. Specialists live in `src/investigation/`. S6 updates the workflow doc's sentence that places the dispatcher in `src/investigation/`. |
| R8 | A `failed` run's report carries status and limitation codes only, with no evidence content. |

## Required security decisions

1. **Trusted handoff only.**
   - Only `InvestigationExecutionRun`, the trusted owner in `src/orchestration/`, establishes the
     proposal-to-execution handoff.
   - It reads the proposal thread only through `load_investigation_state`.
   - `ApprovedPlanHandoff` cannot be constructed from agent output or state alone.
2. **A proposal is never an approval.** A `proposed` record and `awaiting_plan_approval` prove only
   that a plan exists. Execution additionally requires an `InvestigationAuthority` whose `approval`
   came from a trusted adapter.
3. **INV-5 grants nothing.**
   - No INV-5 production module constructs `PlanApprovalRecord` or `InvestigationAuthority`, or sets
     `sql_approval_required` to `False` on anyone's behalf.
   - A structural test enforces this over `src/`, `packages/`, and `apps/`, excluding the defining
     modules.
   - Tests supply authority through fixtures.
4. **The dispatcher has no direct data access.** The dispatcher node calls only the gateway and the
   pure dispatch policy. It never holds `GuardedSQLService`, `DocumentResearchService`, a
   retriever, an executor, or a model client.
5. **Specialists hold only `SpecialistGateway`** (the five port operations). Owner-only operations
   (`resource_status`, `skip_task`, `finish_attempt`, `close`, `evidence_payloads`) are not on the
   port and are banned names in specialist modules.
6. **No accounting bypass.**
   - Starts and retries go through `begin_attempt`, which reserves and counts the attempt.
   - In-run skips and closes go through `skip_task` and `finish_attempt`, which record a decision
     and verify the reason.
   - Every spend is reserved before it runs and settled by the gateway.
   - Budget usage reaches state only as the ledger's own snapshot.
7. **Sequential only.** There is one gateway call per guarded step, with at most one task
   `running`. The `step_writer` channel and the gateway's `concurrent_call` refusal remain the
   backstops. Parallel dispatch is #150.
8. **No content in checkpoints or telemetry.** Rows, SQL, passages, filenames, prompts, model text,
   and report content stay in process. Checkpoints and decisions hold identifiers, counts, and
   fixed codes. INV-5 adds no telemetry or audit field.
9. **Production is fail-closed.** Without a reviewed `BoundedModelAdapter`, every model-executing
   operation is refused with `metering_unbounded` before resolution or reservation. INV-5 does not
   add or loosen an adapter.

## Approval provenance and replay limitations

- **Provenance.**
  - The approval record in state is copied byte-for-byte from `authority.approval` by the trusted
    `approval` step.
  - The owner refuses unless all three digests are equal: `authority.approval.plan_digest`,
    `proposal_record.plan_digest`, and `plan_digest(plan)`.
  - It also refuses unless the scopes are equal (the investigation ID included), the proposal
    status is `awaiting_plan_approval`, and `approval_expired(deadline, clock.now())` is false.
  - Every later gateway call re-checks binding, expiry, the epoch, and the live authority.
- **Replay limitations (documented, not solved in INV-5).**
  - A handoff is single-use per owner object, and a process-local registry keyed by
    `(scope, plan digest, approved_at)` refuses a second execution in the same process.
  - Nothing durable prevents re-execution in another process, or after a restart.
  - `PlanApprovalRecord` carries no approver identity or nonce.
  - Durable idempotency, approver identity, and audit belong to #143/#144. Process-local checkpoints
    still turn every non-terminal investigation into `interrupted` on restart (ADR 0024 D9).
- **SQL approval.** If `authority.sql_approval_required` is true, SQL tasks end
  `refused/policy_refused` (`sql_approval_required`). INV-5 never enters `awaiting_sql_approval`;
  batched SQL approval is INV-10 (#143).

## Gateway and specialist capability boundaries

| Caller | May call | Task kinds and assignee | Never |
|---|---|---|---|
| SQL Analyst (`src/investigation/sql_analyst.py`) | `begin_attempt`, `prepare_sql`, `execute_sql`, `prepare_correction` via `SpecialistGateway` | `sql_analysis` assigned to `sql_analyst` | `research_documents`, owner operations, payloads, ledger |
| Document Research (`src/investigation/document_researcher.py`) | `begin_attempt`, `research_documents` via `SpecialistGateway` | `document_research` assigned to `document_research` | SQL operations, owner operations, payloads, ledger |
| Trusted owner and dispatcher node (`src/orchestration/investigation_execution.py`) | owner-only `resource_status`, `skip_task`, `finish_attempt`, `close(ExecutionCloseReason)`, `evidence_payloads(view)`, plus calling the specialists | routes strictly by `task.kind` and `task.assignee` | constructing authority or approvals; services, retrievers, executors, model clients |
| Supervisor, Business Investigator, Critic | nothing in INV-5 execution | — | everything above |

- **Routing.** A misrouted call is `tool_not_permitted` or `role_not_permitted`, which is
  investigation-terminal. The dispatcher must therefore select the specialist from the admitted
  task and never by trial.
- **Specialist contract.**
  - `step(port, state, last: GatewayResult | None) -> GatewayResult` makes exactly one port call.
  - The current `AttemptToken` and `SQLHandle` stay in process.
  - A specialist never retries, skips, or closes a task.
  - SQL sequence: `begin_attempt → prepare_sql → execute_sql`. If that execution failed with a
    gateway-held correction pending, it continues `prepare_correction → execute_sql`.
  - Document sequence: `begin_attempt → research_documents`.
- **New owner-only gateway operations** (additive; existing operations and tests unchanged). Each
  runs the existing pre-flight (request type, restored and current state, open, not cancelled,
  binding) and the authority check:
  - `resource_status(state)` resolves the table and document index through the trusted resolvers.
    It reserves nothing and records no decision. It returns content-free availability.
  - `skip_task(state, SkipTask(task_id, role, reason))`:
    - `reason` is `dependency_failed` or `resource_unavailable`;
    - the gateway re-verifies the reason (a dependency is terminal and not `succeeded`, or the
      resolver returns `None` now);
    - it moves `pending → skipped` with attempts 0, charges nothing, and records a decision with
      tool `skip_task` (an additive `GatewayTool` literal).
  - `finish_attempt(state, FinishAttempt(task_id, role))` closes a `running` task the dispatcher
    will not retry. The status is `failed`, with the category of the gateway's last recorded
    failure for that task. It revokes the task's handles and pending correction, records a
    `finish_attempt` decision, and charges nothing.
  - `close(reason)`:
    - `reason` is `completed`, `cancelled`, or `shutdown`;
    - the call is idempotent and permanent, and the first closure wins;
    - it revokes every token, handle, and pending correction, and closes the ledger (`completed`,
      `cancelled`, or `policy_refused` respectively);
    - later calls are refused with `gateway_closed`.
  - `evidence_payloads(view)` returns typed payloads only for evidence present in the given
    validated view.

## Sequential task and dependency state machine

The pure policy (`packages/investigation/dispatch.py`) reads a validated `InvestigationStateView`
and returns exactly one next action:

1. **Seed** (owner steps, before dispatch):
   - intake (version and scope);
   - lifecycle `planning`;
   - supervisor writes the handoff plan (unbound writer, digest-checked);
   - lifecycle `awaiting_plan_approval`;
   - approval (copies `authority.approval`);
   - lifecycle `queued`;
   - dispatcher registers every admitted task as `pending`;
   - lifecycle `running`.
2. **Resources.** One `resource_status` call. Each document task whose resource is unavailable gets
   `skip_task(resource_unavailable)`, one step each, and no attempt is charged.
3. **Loop.** Each iteration takes exactly one of these actions, in this priority order:
   - a. A `running` task with a pending specialist operation runs `specialist.step` (one port call).
   - b. A `running` task whose last gateway result left it running gets `begin_attempt` if retry
     is eligible, and `finish_attempt` otherwise.
   - c. A `pending` task with any dependency terminal and not `succeeded` gets
     `skip_task(dependency_failed)`; this repeats transitively.
   - d. The first `pending` task, in plan order, whose dependencies all `succeeded` gets
     `begin_attempt` from its specialist.
   - e. When no task is `pending` or `running`, the run is finished (see "Final-status precedence").
4. **Invariants.**
   - At most one task is `running` (checked by the policy and asserted in tests).
   - A cancellation check runs on every edge before an action.
   - Every gateway result's `updates` are returned as issued.
   - A `GatewayResult` with an investigation-severity refusal ends the loop.
   - A bare refusal (`state_invalid`, `concurrent_call`, with empty updates) ends the run in
     process as `failed`.
5. **Step bound.** LangGraph's default `recursion_limit` (25) is too small. The owner passes an
   explicit limit derived from the caps: seeding (8) + resources (1 + tasks) + attempts × 5 + tasks
   (finish) + closing (4), asserted by a test. Hitting it is a defect: the owner closes the gateway
   (`shutdown`) and reports `failed`.

## Retry eligibility and budget ceilings

- **When a retry is allowed.** A second attempt needs both of the following:
  - `task.max_attempts == 2`;
  - a last failure category in `{provider_failure, retrieval_or_grounding, sql_generation,
    internal}`.

  `timeout`, `unsafe_query`, `policy_refused`, and `provider_configuration` are never retried. The
  SQL correction is not a retry: it is the gateway-counted single correction inside one attempt.
- **Ceilings.** The ledger is `BudgetLedger(limits)`, where `limits` defaults to `EXECUTION_LIMITS`.
  The owner refuses any limits field above `EXECUTION_LIMITS` (tests may only lower them). INV-5
  uses no `replan_rounds` or `critic_rounds`.
- **Per-operation reservations** (unchanged from INV-3):

  | Operation | Reserves |
  |---|---|
  | `begin_attempt` | 1 task attempt, settled immediately |
  | `prepare_sql` | 1 envelope call |
  | `prepare_correction` | 1 envelope call |
  | `research_documents` | 1 retrieval + 1 envelope call |
  | `execute_sql` | 1 SQL execution |
  | Owner operations | nothing |

  So a SQL attempt makes at most 2 model calls and 2 executions; a document attempt makes 1 model
  call and 1 retrieval.
- **Exhaustion.** Exhaustion of any resource (wall clock included, 240 s by default) refuses with
  `budget_exhausted` or `budget_closed`. That closes the ledger with `exhausted` and ends the run as
  `budget_exhausted`.

## Cancellation and late-result rules

- **Before each action.** The owner checks the cancellation token before every action. An
  unreadable token counts as cancelled. On cancellation:
  1. `close(cancelled)`;
  2. the dispatcher writes the closed ledger snapshot and closing bookkeeping (pending →
     `cancelled`, running → `cancelled`, category `cancelled`);
  3. lifecycle `cancelled`;
  4. terminal record.

  No further `begin_attempt` or port call happens.
- **During a call.** The gateway already re-checks cancellation after the service returns. A
  result is late if that check sees the cancellation. A late result:
  - charges its reservations (the service was entered);
  - stores no evidence and no payload;
  - is refused with `cancelled`, which closes the gateway.

  A result the gateway issued before that check completed before cancellation and is kept.
- **After terminal status.** A call into a closed gateway is refused with `gateway_closed`. Owner
  operations after closure are idempotent no-ops. Payloads of evidence absent from state are never
  returned.

## Evidence and citation validation

- **State records** (unchanged): IDs are gateway-issued (`ev_` plus 32 hex characters), carry
  `task_id` and `attempt`, belong to a non-pending task, and match the scope's dataset or
  collection.
- **Typed payloads** (`packages/investigation/evidence.py`, built by the gateway at issuance;
  process-local, frozen, never checkpointed):
  - `QueryEvidencePayload`:
    - `evidence_id`, `task_id`, `attempt`, and column names;
    - rows of JSON scalars (`str`, `int`, finite `float`, `bool`, `None`), at most 500 (the
      executor's limit), with a `truncated` flag;
    - no SQL (the SQL stays in the gateway).
  - `DocumentEvidencePayload`:
    - `evidence_id`, `task_id`, `attempt`;
    - sources (number, filename, page, chunk, score);
    - cited source numbers, invalid citations, and evidence statuses;
    - the answer text, typed as untrusted model output.
  - `DerivedEvidencePayload` (contract only, R3): a closed operation set and source evidence IDs.
- **`verify_payload(record, payload)`** requires equal ID, task, attempt, and kind. For query
  evidence the row and column counts must equal `QueryProvenance`. For document evidence the source
  numbers, citation count, invalid count, and statuses must equal `DocumentProvenance`. A mismatch
  excludes the payload and adds the limitation `evidence_inconsistent`.
- **Valid citations** require all of the following:
  - at least one cited source;
  - every cited number among the payload's sources;
  - `invalid_citation_count == 0`;
  - no `INVALID_REFERENCE` or `QUOTE_MISMATCH` status.

  Otherwise the evidence is reported only as the limitation `document_evidence_unverified`.

## Partial failure and final-status precedence

Evaluate the following in order; the first match wins.

1. A node error, a poisoned thread, a bare gateway refusal, or the step bound: `failed` in process.
   The checkpoint may be unreadable, and it never resumes.
2. The first investigation-severity refusal that closed the gateway: its `LIFECYCLE_TARGET`.
   - `cancelled` → `cancelled`;
   - `budget_exhausted` and `budget_closed` → `budget_exhausted`;
   - all others → `failed`, including `approval_expired`, because `running → expired` is illegal.
3. Cancellation observed by the owner between actions: `cancelled`.
4. Normal end:
   - every task `succeeded` → `completed`;
   - at least one task `succeeded` and at least one not → `completed_partial`;
   - zero succeeded → `failed` (R2).

The `TerminalRecord.category` is the category of the first non-succeeded task in plan order, or
`None` for `completed`. The interim report is composed for `completed`, `completed_partial`,
`budget_exhausted`, and `cancelled`. For `failed` it carries codes only (R8).

## Strict checkpoint and ledger ownership

- **No change to the channel schema.** `STATE_VERSION` stays 1. The only state edit is the
  additive `GatewayTool` literal `"skip_task"`. An older reader refuses a state that contains it
  rather than misreading it.
- **Writers.**
  - The execution graph registers intake, lifecycle, supervisor (plan seeding only, unbound),
    approval, dispatcher (bound to `gateway.ledger`), and terminal.
  - No proposal-record writer. No raw nodes.
  - Each step returns one owner's channels.
- **The execution ledger is constructed by the owner** and passed to the gateway. It is never the
  planning ledger, and the owner closes it only through `gateway.close`.
- **After a terminal status** the dispatcher may write only the closed ledger snapshot and closing
  task bookkeeping. `restore_state` must pass at every checkpoint of every test run.
- **Checkpoints stay process-local**, on the strict saver. Durable checkpoints are #149.

## Interim report (deterministic, no model call)

`compose_interim_report(view, payloads) -> InterimReport` lives in
`packages/investigation/interim_report.py`. It is pure and framework-neutral. It contains:
- the status and the label "interim, unverified, association only";
- one row per task: ID, kind, status, and category;
- query tables, at most 50 rows each, built from verified payload cells only (it computes no new
  numbers);
- verified citation locations (filename and page) only, never the answer prose as a finding;
- limitations from fixed templates keyed by outcome codes.

Identical inputs give byte-identical output. The report is in process only; persistence belongs to
#144.

## Stages and exit criteria

| Stage | Scope | Exit criteria |
|---|---|---|
| S0 | This brief | Reviewed against the source and #138; docs checks pass; approved separately |
| S1 | `evidence.py`, `dispatch.py`, `interim_report.py` contracts and pure functions; additive `GatewayTool` literal; exports | Baseline recorded first; contract, JSON round-trip, and pre-INV-5 restore-compatibility tests; `check_all` green |
| S2 | Gateway seams: typed payload issuance, `evidence_payloads`, owner-only `resource_status`, `skip_task`, `finish_attempt`, `close`; port unchanged | New gateway tests; bypass tests show the port lacks owner operations; boundary-test name additions; every existing gateway test unchanged and green |
| S3 | `sql_analyst.py`, `document_researcher.py` | AST boundary test; scripted single-task runs through the real gateway (success, correction, timeout, absent index) |
| S4 | `investigation_execution.py`: handoff verification, seeding, dispatch loop, cancellation edges, termination, terminal record, step bound, shutdown on exceptions | Lifecycle, cancellation, budget, timeout, missing-index, expiry, revocation, replay, and restore-every-checkpoint tests; no-approval-construction structural test |
| S5 | Interim report composer and the scripted churn acceptance test | Acceptance matrix below; determinism; injection fixture excluded from findings |
| S6 | Docs: ADR 0024 Amendment 3 (including the R4 policy-criteria clarification and R7 placement), `docs/architecture/investigation-workflow.md`, this brief's readiness section | `check_all` green; evaluations unchanged; readiness review |

## File-level implementation plan

**New**
- `packages/investigation/evidence.py`, `packages/investigation/dispatch.py`,
  `packages/investigation/interim_report.py`
- `src/investigation/sql_analyst.py`, `src/investigation/document_researcher.py`
- `src/orchestration/investigation_execution.py` (`ApprovedPlanHandoff`,
  `InvestigationExecutionRun`, `ExecutionResult`)
- tests:
  - `tests/investigation_execution_fakes.py` (scripted `BoundedModelAdapter`, resolvers, a timing-out
    executor, a cancelling adapter)
  - `tests/test_investigation_evidence_payloads.py`
  - `tests/test_investigation_dispatch_policy.py`
  - `tests/test_investigation_specialists.py`
  - `tests/test_investigation_execution_graph.py`
  - `tests/test_investigation_execution_lifecycle.py`
  - `tests/test_investigation_interim_report.py`
  - `tests/test_investigation_churn_execution.py`

**Modified (additive)**
- `src/orchestration/tool_gateway.py`
- `packages/investigation/state.py` (the `GatewayTool` literal only)
- `packages/investigation/__init__.py`, `src/investigation/__init__.py`
- `tests/test_investigation_boundaries.py`: add the new trusted modules to `RUNTIME_MODULES`; ban
  `resource_status`, `skip_task`, `finish_attempt`, `evidence_payloads`, `ExecutionCloseReason`,
  `ApprovedPlanHandoff`, and `InvestigationExecutionRun` in specialist modules
- `tests/test_investigation_bypass.py`
- S6 only: `docs/adr/0024-multi-agent-investigation.md`, `docs/architecture/investigation-workflow.md`

## Do not touch

`apps/**`, `apps/web/**`, OpenAPI, `evals/**` and every baseline, INV-2 fixtures and
`sample_data/**`, `src/orchestration/langgraph_orchestrator.py` and the legacy routes,
`src/orchestration/guarded_sql.py` and `src/orchestration/document_research.py` (reused unchanged),
the proposal graph and planning gateway, production provider clients and `src/llm/bounded.py`
admission rules, telemetry and audit allowlists, dependency and lock files, containers. No routing
(#142), API, or approval source.

## Scripted churn acceptance matrix

| # | Criterion (#138) | Setup | Assertion |
|---|---|---|---|
| A1 | Segment results equal the reference SQL | Real `sample_data/subscription_churn.csv` in SQLite. Plan: `trend → segments`, plus `pricing` and `playbook` document tasks. The scripted adapter returns the test-loaded `overall_churn` and `segment_plan_churn` SQL. | Query payload rows equal the evaluator's `reference.sql` run and `oracle.json` `reference_sql_results` (counts exact, rates within 0.0001). Status `completed`. |
| A2 | Citations are valid | Real four-PDF index (deterministic term embedder); scripted cited answers | `subscription_churn_pricing_change_notice.pdf` page 1 is cited; citation rules hold; `verify_payload` passes |
| A3 | SQL timeout → `completed_partial` | Executor raises `QueryTimeoutError` for `trend` | `trend` `failed/timeout`, not retried; `segments` `skipped/dependency_failed`; document tasks succeed; limitation stated |
| A4 | Missing index → skipped plus limitation | Document resolver returns `None` | Document tasks `skipped/resource_unavailable` with attempts 0; no retrieval reserved; `completed_partial` |
| A5 | Tiny budget → `budget_exhausted` | `task_attempts=1` (plus a wall-clock variant) | Second `begin_attempt` refused; ledger `exhausted`; partial report; remaining tasks closed |
| A6 | Cancellation stops dispatch | Token set between tasks | No later port call (spy); ledger `cancelled`; status `cancelled` |
| A7 | Late results discarded | Token set inside the adapter or executor call | No evidence or payload for that call; reservation charged; `cancelled` |
| A8 | No execution without approval | Digest, scope, status, or expiry mismatch; reused handoff | Refused before any gateway call; nothing seeded |
| A9 | Content stays out of state | Every run above | Checkpoint blobs and decisions contain no row value, SQL, filename, or passage; `restore_state` passes at every checkpoint |
| A10 | Production fail-closed | Production-style client (not an adapter) | `metering_unbounded`; nothing resolved or reserved |
| A11 | Prompt-injection memo | Index includes `subscription_churn_draft_board_memo.pdf` | Memo text never creates a task or changes routing; never appears as a finding |

Evaluator-only files are read only by tests. The runtime boundary and oracle scans stay green.

## Deferred production dependencies (outside INV-5)

- a reviewed real-provider bounded adapter and its evidence;
- routing behind the flag (#142);
- approvals, audit, approver identity, durable idempotency, and SQL approval waves (#143 / INV-10);
- API, jobs, and durable report storage (#144);
- Business Investigation, hypothesis tests, and derived evidence production (#139 / INV-6);
- Critic and replanning, plus evidence-citing follow-ups and document-derived policy criteria
  (#140 / INV-7, per R4);
- verified report (#141);
- observability (#146);
- evaluation (#145, #147, #148);
- durable checkpoints (#149);
- parallel dispatch (#150);
- UI (#155, #156);
- security review (#157);
- deployment (#159).

## Evaluation and docs impact

No change to `evals/**` or any baseline, and no measured case is removed, demoted, or worsened. No
new telemetry or audit field. S6 adds ADR 0024 Amendment 3 (the R4 clarification that
document-derived policy criteria move from INV-5 to the first evidence-citing milestone, plus R7
placement) and updates the workflow document.

## Remaining design questions (non-blocking; resolve in the stage named)

- **S2:** whether `finish_attempt` uses the gateway's last recorded failure category or the latest
  `failed` decision in state. Recommended: the gateway record, with a test that both agree.
- **S4:** the exact step-bound formula constant, fixed by test against `EXECUTION_LIMITS` and
  `MAX_PLAN_TASKS`.
- **S5:** the report's limitation wording, which is fixed text keyed by code and reviewed once.

## S0 acceptance

- [x] `origin/main` verified at `7c7d8d5`, containing the INV-4 PR #166 merge.
- [x] #138, ADR 0024, the workflow doc, INV-0 to INV-4 contracts, the gateway, the INV-1 services,
      the churn fixture and its oracle isolation, and the boundary tests reviewed.
- [x] Stages, files, decisions R1–R8, security decisions, state machine, retry and budget rules,
      cancellation, evidence validation, status precedence, ownership, acceptance matrix, and
      deferrals documented.
- [x] No contract, gateway, specialist, graph, test, evaluation, ADR, or GitHub change made in S0.

## Readiness (S6, 2026-10-19)

Delivered locally on `feat/iteration2-inv5-dispatcher-specialists`, one commit per stage:

| Stage | Delivered |
|---|---|
| S1 | `evidence.py`, `dispatch.py`, `interim_report.py` contracts and pure policy |
| S2 | Owner-only gateway operations, typed payload issuance, the `skip_task` literal |
| S3 | `SQLAnalyst` and `DocumentResearcher` |
| S4 | `InvestigationExecutionRun`: handoff, seeding, dispatch, cancellation, defects, step bound |
| S5 | `compose_interim_report` and the scripted churn acceptance matrix |
| S6 | ADR 0024 Amendment 3, the workflow document, this section |

Approved changes to the decisions above (2026-10-19), carried through every stage and documented
in Amendment 3:

1. Retry eligibility is limited to `sql_generation` and `retrieval_or_grounding`; `provider_failure`
   and `internal` are unknown outcomes and are never retried (refines R6). The execution gateway
   enforces the same set (`retry_categories`).
2. A retry also needs an open ledger and a shared task attempt left; otherwise the task is finished.
3. The `skip_task` literal was added in S2 with its gateway operation, not in S1.
4. Payload verification compares the complete source location (number, page, chunk, and score).

Resolved design questions: `finish_attempt` uses the gateway's failure record and refuses unless the
last state decision agrees (`state_invalid`); the step bound is `8 + 1 + Σ(max_attempts × 5 + 1) +
4`, pinned by a test; limitation wording is fixed in `LIMITATION_TEMPLATES`.

Acceptance: A1, A2, A3, A4, A5, A9, A11, and determinism in
`tests/test_investigation_churn_execution.py` on the real churn CSV and four-PDF index; A6, A7, A8,
and A10 in `tests/test_investigation_execution_lifecycle.py`.

Known limits: process-local replay protection only; no approver identity; checkpoints are
process-local and become `interrupted` on restart; production stays fail-closed until a reviewed
bounded adapter exists; the deferred dependencies above are unchanged.
