# Investigation workflow

How the multi-agent investigation fits beside the existing question graph. The decisions are in
[ADR 0024](../adr/0024-multi-agent-investigation.md), and delivery is tracked by
[#131](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/131). Status
today: the contracts in `packages/investigation/` exist (INV-0). The question graph runs its SQL and
document steps through `GuardedSQLService` (`src/orchestration/guarded_sql.py`) and
`DocumentResearchService` (`src/orchestration/document_research.py`), which specialists will reuse
(INV-1). INV-3 (#136, stages S0 to S4) adds typed JSON-native state with validated single-writer
updates (`packages/investigation/state.py`), the gateway refusal and authority vocabulary
(`refusals.py`, `gateway.py`), and strict process-local checkpoints
(`src/orchestration/investigation_checkpoint.py`). S5 adds the tool gateway's guarded attempts and
document research (`src/orchestration/tool_gateway.py`). SQL handles (S6) and bypass enforcement
(S7) are not built yet, and nothing below runs in a route or graph yet. INV-4 (#137) adds the
proposal phase and INV-5 (#138) sequential execution of an approved plan (see "Execution phase
(INV-5)"); routing stays off (#142).

These services are not access controls. The API authorizes the caller, enforces consent, and
resolves the tenant's table and documents before building them. For investigations, the INV-3 tool
gateway must apply tenant binding, consent, capability, budget, and cancellation before any call
reaches them. Approval interrupts, the correction count, and hybrid fallback stay in the graph.
The extraction preserves behaviour exactly, with one intentional exception: resuming an approval
when the orchestrator has no table now fails with `SQLAgentError` before execution, instead of an
assertion error (see the #134 brief).

## Topology

```text
Question graph (existing routes unchanged)
route ─┬─ memory | sql | rag | hybrid | unsupported
       └─ investigation (feature flag, off by default)
            PROPOSAL PHASE — planning budget, no tools
            intake (schema, document titles, consent, resources)
            → supervisor_plan → validate_plan (≤1 repair) → cost_estimate
            → proposal shown; execution waits for explicit approval

Approved plan → background job → EXECUTION SUBGRAPH — execution budget
   dispatch (sequential; only tasks whose dependencies are complete)
     → sql_analyst | document_research      (through the tool gateway)
       [batched SQL approval interrupt when required]
   → collect → business_investigator: hypotheses → hypothesis tests → verdicts
   → critic → supervisor_replan (≤2 rounds; follow-ups re-validated) → dispatch …
   → compose_narrative (≤1 bounded call) → finish → compose_report → verify_report → terminal
```

## Roles

| Role | Does | Executes tools |
|---|---|---|
| Supervisor | Plans, validates and admits follow-ups, replans within budget | No |
| SQL Analyst | `sql_analysis` and table hypothesis tests through the guarded SQL service | Yes |
| Document Research | `document_research` and `document_check` tests through retrieval and RAG | Yes |
| Business Investigation | Proposes hypotheses and tests; issues verdicts that cite evidence | No |
| Critic | Flags unsupported claims, contradictions, and gaps; requests ≤3 follow-ups per round | No |

## Shared state ownership (INV-3)

| State | Single writer | Update rule |
|---|---|---|
| Plan, admitted follow-ups, round counts | Supervisor | Only validated tasks are admitted |
| Task status | Dispatcher, applying gateway-issued updates | Moves forward only: pending → running → terminal |
| Evidence | Dispatcher, applying gateway-issued records | Insert-only; IDs assigned by the gateway |
| Hypotheses and verdicts | Business Investigation | Verdicts must cite existing evidence |
| Critiques (INV-7) | Critic | Append-only, one per critic round (0, 1); content-free codes and IDs |
| Follow-up rounds (INV-7) | Supervisor | Append-only (1, 2); each bound to its critique, findings, and an approval of its exact task set |
| Budget usage | Dispatcher, applying gateway ledger snapshots | Cumulative; never reset |
| Proposal record (INV-4) | Supervisor, bound to the planning ledger | Written once while `planning`; equals the closed planning ledger; matches the plan digest |
| Approvals | API resume path | Typed decisions only |
| Report and terminal status | Composer and terminal nodes | Written once |

The tool gateway is authoritative for task attempts, evidence IDs, and budget usage, and it never
writes state itself. It returns typed updates, and the dispatcher node applies them. Every update is
validated inside the writing node (`owned_update`) before LangGraph's reducers see it, because a
reducer that rejects an update leaves it stored as a pending write and makes the thread unreadable.
The same check enforces status: tasks start or retry, evidence is created, and budget is spent only
while the investigation is `running`. After a terminal status only bookkeeping is accepted (closing
tasks, recording refusals, settling reservations made while running, and the terminal record).

**Budget settlement comes from the gateway's ledger.** State records only how many reservations
are outstanding, not their amounts, so on its own it cannot check that a settlement charges no more
than it releases. The dispatcher's node is therefore bound to the gateway's `BudgetLedger` when the
graph is built (`guarded_node(..., ledger=gateway.ledger)`). In every status, a change to budget
usage needs that binding and must equal the ledger's snapshot exactly, so the dispatcher can never
record consumption the ledger did not. After a terminal status the ledger must also be closed, so it
cannot authorize another reservation. Binding the right ledger to the right investigation is trusted
wiring: state cannot tell two ledgers apart, and `restore_state` checks only shape and monotonicity.

**Writers run one at a time.** Each update is validated against the state before its step, and
reducers see one channel at a time. Two writers in the same LangGraph step would each pass alone,
and their combination (for example `completed` plus a new task start) would never be checked:
restore cannot tell it apart from legitimate closing bookkeeping. `guarded_node` therefore also
writes `step_writer`, a channel with no reducer. LangGraph refuses two writes to it in one step
(`InvalidUpdateError`), and the thread then fails closed (`checkpoint_unreadable`). This rules out
parallel guarded writers, including parallel specialists, until a later design adds cross-channel
validation. It does not make one step's writes atomic across channels. A node added without
`guarded_node` is outside every one of these checks, so graph wiring must guard every node.

## Lifecycle

`planning → awaiting_plan_approval → queued → running ⇄ awaiting_sql_approval`, ending in exactly one
terminal status: `completed`, `completed_partial`, `budget_exhausted`, `plan_invalid`,
`plan_rejected`, `cancelled`, `failed`, `interrupted`, or `expired`. Cancellation, failure, and
restart interruption are reachable from every live status. Execution is reachable only through plan
approval. Plan approval expires after 24 hours, or when the workspace expires if that is sooner.

## Contracts in `packages/investigation/` (INV-0)

- `contracts.py`:
  - five `AgentRole`s and three `TaskKind`s, with fixed proposer and executor maps;
  - typed task inputs, `PlanTask`, and `InvestigationPlan` (`schema_version` 1);
  - a content-free `TaskOutcome` and `EvidenceRef`.
- `validation.py`: deterministic checks on plans and follow-up rounds. They return violation codes
  and task IDs only.
- `budget.py`: `PLANNING_LIMITS` and `EXECUTION_LIMITS`; a fail-closed `BudgetLedger` driven by a
  monotonic clock; and `estimate_cost`, which returns a `CostEstimate` labelled
  `enforced_upper_bound`, `indicative`, or `unavailable`.
- `lifecycle.py`: statuses, legal transitions, and the approval deadline.
- `planning.py` (INV-4 S1): a trusted, scope-bound planning context exposes only the question,
  schema names/types/descriptions, and document display titles to a future planning adapter. Schema
  descriptions, titles, and the question remain bounded untrusted data, never instructions or
  authority. Its strict draft validator reuses `validate_plan`; its proposal wrapper preserves the
  embedded plan and digest, keeps missing-information text informational, and distinguishes plan
  approval from SQL approval deferred to statement preparation.

The package imports no LangGraph, `src`, `apps`, or web framework
(`tests/test_package_boundaries.py`).

## Milestones

| Milestone | Issue |
|---|---|
| Contracts (this document) | #133 |
| Guarded service extraction | #134 |
| Churn fixture | #135 |
| State and gateway | #136 |
| Proposal phase | #137 |
| Specialists | #138 |
| Business Investigation | #139 |
| Critic and replanning | #140 |
| Verified report | #141 |
| Routing | #142 |
| Approvals and audit | #143 |
| API and jobs | #144 |
| Evaluation | #145, #147, #148 |
| Observability | #146 |
| Durable checkpoints | #149 |
| Parallel dispatch | #150 |
| UI Stage 6b/6c | #155, #156 |
| Security | #157 |
| Deployment | #159 |

## Tool gateway execution (INV-3 S5)

`InvestigationToolGateway` (`src/orchestration/tool_gateway.py`) serves one sequential
investigation. Specialists send it typed, frozen requests: `BeginAttempt(task_id, role)` and
`ResearchDocuments(task_id, role, attempt)`. Requests carry no query, prompt, or resource name; the
query and passage limit come from the admitted plan task, and the document collection comes from
the trusted resolver for the authority's scope.

Every call runs one pre-flight and stops at the first failure with a code-only refusal. The order is:

1. Request type.
2. Restored state, which must be the state this gateway last issued updates for.
3. The gateway is open.
4. The investigation is not cancelled.
5. Scope, approval, and `running` binding.
6. The task is admitted and at the right step, the role is the assigned executor, the tool fits the
   task, and the attempt token is the one issued.
7. The provider envelope is bounded, and the model client is an admitted `BoundedModelAdapter`
   whose declared bound is the envelope's only member (`metering_unbounded` otherwise).
8. Capability, consent, approval expiry, then the live re-check.
9. Tenant resolution.
10. A worst-case ledger reservation for the retrieval and the whole provider envelope.

Model calls go through an admitted client that allows one call per reservation. Before anything is
sent, it asks the adapter for the exact input-token count of the complete request (system text,
template, and prompt) and refuses a count above the input cap, or one that is not an integer. It
sends the envelope's output cap with the request and meters the provider's reports, including who
served the call.

The retrieval and the model call have separate reservations, and both are settled in every outcome
after the service wrappers are sealed. A charge is never below what was reported.

**Proving that no call happened.** Each wrapper has an invocation marker. The marker is set
immediately before the wrapped service is entered, and only the wrapper can reach the service for
that reservation. The meter is installed from the input count onward. The gateway therefore treats
the model as *provably not invoked* only when all three hold: the marker is unset, the meter saw no
report of any kind (usage, fallback, or served-by), and the meter did not fault. A missing usage
report alone never counts as proof.

| Outcome | Model reservation | `reservation_bound` effect |
|---|---|---|
| No relevant passage; retrieval failed or was cancelled before the model; prompt refused before the call (count over the cap, not an integer, or the counter raised) | Released: no model calls or tokens charged | None (`released_reservations` + 1) |
| Budget refused the model reservation | Never issued; the retrieval reservation is released too | None |
| Invoked, one complete report within the contract and the reservation | Charged as reported | `held` (`verified_model_settlements` + 1) |
| Invoked, provider raised with no report | Full reservation | `unverified` (`unmetered_model_calls` + 1) |
| Uncertain: provider activity without an invocation, or a meter fault | Reservation or reported totals, whichever is larger; `metering_violation` | `unverified` |
| Contract breach: a second call, a retry, a fallback, another provider or model, more input than counted, more output than the cap, or a success without complete usage | Same as uncertain; `metering_violation` | `unverified`, or `exceeded` if reports pass the reservation |

A `metering_violation` discards the evidence, closes the gateway and its ledger, and sends the
investigation to `failed`. `BudgetLedger.settle` never treats usage above a reservation as successful
accounting. It charges the reported amount, records `exceeded_reservations`, closes with
`reservation_exceeded`, and raises `ReservationExceededError`.

The retrieval reservation is charged once the retriever has been entered, and released if it never
was.

### Bound terminology

These terms are kept apart, and each claims only what its evidence supports:

| Term | Meaning | Evidence |
|---|---|---|
| **Declared limits** | What the adapter (`BoundedModelAdapter.bound`) and the authority's `ProviderEnvelope` say: one attempt, an output cap, bounded input, a fixed provider and model. | A code-level claim, plus the check that the envelope equals the adapter's declaration. Admission proves only that the two agree. |
| **Enforcement at the request boundary** | What the gateway itself does before and during the call: refuses a prompt whose exact count passes the cap; sends the cap with the request; allows one `generate_once` per reservation; seals the wrapper afterwards; refuses an answer from another model. | Gateway code. Whether the *provider* honours the cap, and whether the adapter's count is exact, cannot be seen before the call. They are checked only against the reports afterwards. |
| **Complete observed usage within reservation** (`held`) | At least one model settlement had a complete report, inside the contract and the reservation, and no settlement was unknown or exceeded. | Provider reports, checked by the gateway and charged as reported. |
| **Unknown usage** (`unverified`) | A settlement where an invocation happened, or may have happened, but usage is not completely known. | Charged at the reservation or more. This is an upper bound only if the provider really enforced the declared caps. |
| **Confirmed reservation overrun** (`exceeded`) | Reported usage passed what the reservation authorized. | Provider reports, charged as reported. Final: the count can never decrease in state. |
| **No evidence** (`none`) | No model settlement yet. Released reservations do not count. | None. Nothing is claimed. |

`held` is never assigned because an adapter was admitted, or because a call was released.
`CostEstimate.bound == "enforced_upper_bound"` is a separate, earlier estimate about provider
profiles. It is not evidence about any settlement. For planning, it also requires trusted
`ReviewedPlanningEvidence` covering every envelope member exactly; pricing flags and envelope
declarations alone give at most `indicative`.

**Current production providers remain unsupported for model-executing investigation tools.** No
production client is a `BoundedModelAdapter`, so document research and any other model-executing
investigation tool is refused with `metering_unbounded` before any resource is resolved or budget
reserved.

**Planning (INV-4 S2c/S2d).** `InvestigationPlanningGateway`
(`src/orchestration/investigation_planning.py`) makes one initial planning call and, only after an
invalid initial candidate, at most one repair. Before each call it checks cancellation, the
ledger's remaining time, and `check_planning_authority`, then admits only a
`DeadlineBoundedModelAdapter` whose single envelope member `ReviewedPlanningEvidence.covering`
matches exactly (`admit_planning_adapter`). Each call reserves its full 6,000/2,000 stage (INV-6 D2) on the
same `BudgetLedger(PLANNING_LIMITS)` before exact counting, and runs with what remains of the
original 30-second deadline. The repair must use exactly the trusted inputs the initial call was
bound to, and `render_repair_prompt` over the same metadata (equal `metadata_digest`) and the
initial attempt's fixed validation codes and task IDs; the raw initial candidate is never kept.
Settlement follows the tool gateway's conservative rules, and additionally requires the reported
input to equal the exact count. Two invalid candidates end in `plan_invalid`; every refusal and
every illegal sequence (`planning_sequence_violation`) is terminal, so no third call is possible.
No production client or reviewed evidence exists, so production planning is refused with
`metering_unbounded` before the prompt is rendered.
A count above a stage's 6,000-token input partition closes the ledger `exhausted` without naming
`exhausted_resource`: no tokens were charged, and the 6,000 limit is the stage's partition, not the
ledger's limit, so the refusal code `budget_exhausted` carries the cause (kept as is in S2e).

The Supervisor holds only `PlannerPort` (`initial()` and `repair()`). The trusted lifecycle owner
that built the gateway (in S3/S4, the proposal-graph node that owns the planning step) is the only
caller of `close(PlanningCloseReason)`, for an abandoned repair, cancellation, revoked authority, an
expired planning deadline, or shutdown. Closure is idempotent and permanent, makes no model call,
never resets the ledger or deadline, and seals an in-flight call so its answer is discarded and
settled by that call. `planning_lifecycle_target` maps a result to the status the owner may set:
a proposal reaches only `awaiting_plan_approval`, never `queued` or `running`.
Once a proposal is published the gateway is terminal: later calls and closes return
`gateway_closed` (no new status) and cannot cancel or fail it. Withdrawing a proposal that awaits
approval belongs to the investigation lifecycle owner, not to the planning gateway.

**Supervisor (INV-4 S3).** `src/investigation/supervisor.py:plan_investigation(port)` calls
`initial()` once and, only after an `invalid` initial result, `repair()` once, returning the
gateway's result unchanged. It checks every result's type, shape, and stage-legal status before it
can cause another call; a malformed result or a raising port ends with a fixed
`SupervisorFailureCode` (`supervisor_result_invalid`, `supervisor_port_error`) and no further call,
which the S4 lifecycle owner records as `failed` after closing the gateway. It imports only the
port, the result types, and the pure planning enums.

**Proposal graph (INV-4 S4).** `InvestigationProposalRun`
(`src/orchestration/investigation_proposal.py`) is the trusted lifecycle owner of one planning step.
It builds the planning gateway from server-side inputs and compiles a proposal-mode graph with
`InvestigationGraphBuilder`, which in that mode registers only `intake`, `supervisor`, and
`lifecycle` steps, binds the Supervisor writer to the planning ledger, and refuses any dispatcher
(no execution ledger, no tool gateway); an execution graph, conversely, refuses any ledger-bound
Supervisor writer, so it can never write a planning record. The steps are intake (version and
scope), lifecycle (`planning`), Supervisor (`plan_investigation` with `planner_port()` only, then
the plan and a `proposal_record` in one guarded update), and lifecycle (the record's target).
`proposal_record` is a write-once, Supervisor-owned channel: outcome, a fixed code, the lifecycle
target, the plan digest for a proposal, the closed planning ledger's `BudgetUsage` (which must equal
the bound ledger), and the `CostEstimate`; never prompts, candidates, missing-information text, or
errors. State invariants tie a proposed record to the stored plan's digest, keep a failed planning
step at its own target, and allow the record only while `planning`. The owner closes the gateway
(`abandoned` on a Supervisor failure, `shutdown` on an exception) before recording. A proposal
reaches `awaiting_plan_approval` only; queuing and running still require a plan approval the
proposal graph cannot write. `STATE_VERSION` stays 1: the channel is optional, existing states
restore unchanged, and checkpoints are process-local.

### Supported model clients

The gateway runs model calls only for a `BoundedModelAdapter` (`src/llm/bounded.py`). No production
client is one, so every model-executing investigation path is disabled today and refused with
`metering_unbounded` before any resolution or reservation. The question workflows keep their
current clients unchanged.

| Client | Why it is refused |
|---|---|
| `GeminiClient` | Purpose-tier model routing; internal retries (`max_retries=2`); no output cap unless configured; a system instruction outside the prompt; no local tokenizer for an exact pre-call count. |
| `AnthropicClient` | Purpose-tier routing; internal retries; a system instruction outside the prompt; no pre-call count. |
| `OllamaClient` | Purpose-tier routing; internal retries; no output cap is sent; the server's model template wraps the prompt with text the client cannot see. |
| `FallbackLLMClient` | Any member may serve the call. |
| `ObservedLLMClient` and other wrappers | A wrapper is not the adapter, even around one. |

UTF-8 byte length is not used as a token bound. The provider adds system and template tokens that
the prompt's bytes do not contain. A normalising tokenizer can also produce more tokens than bytes:
under NFKC, the 3-byte U+FDFA becomes 18 characters (33 bytes).

An overlapping or re-entrant call is `concurrent_call`. It closes the gateway, and the in-flight
call also ends refused. Investigation-severity refusals close the gateway and its ledger and name the
lifecycle target. Results carry JSON updates for the ledger-bound dispatcher: decisions, task
records, evidence metadata (collection, source number, page, chunk index, score, citation counts,
and evidence statuses), and the ledger snapshot. Passages, filenames, prompts, model output, and
provider error messages stay out of results and state. The research payload stays in process; INV-5
exposes it to the trusted owner only as a typed payload (`evidence_payloads`).

Limitations: an adapter's exact count, single attempt, and provider-enforced cap are a reviewed,
code-level claim that subclassing `BoundedModelAdapter` makes. The gateway verifies identity
against the envelope before the call and every report after it. It cannot see a provider request
that is never reported, and it cannot recover money already spent when a breach is detected; it
can only charge the reported amount, mark the bound `exceeded`, and stop. Fallback chains and
multi-attempt envelopes are not supported for investigations.

## SQL handles (INV-3 S6)

The gateway alone prepares and executes investigation SQL, in three operations. Each takes a task
ID and role, plus a gateway-issued token; none takes SQL, a question, a resource, a correction
counter, or an error text (`extra="forbid"`, strict).

- `PrepareSQL(task_id, role, attempt)` uses the attempt token once. The question comes from the
  admitted task: a `sql_analysis` objective and fields, or the method and fields of a
  non-document `hypothesis_test`. Any other task kind is `tool_not_permitted`. The gateway then
  runs the S5 pre-flight and model path:
  1. it refuses `sql_approval_required` if the authority needs approval;
  2. it admits the bounded adapter (`metering_unbounded` otherwise) and re-checks live authority;
  3. it resolves the tenant's table through the trusted `TableResolver`;
  4. it reserves the worst case of one logical call, and makes one bounded call through
     `GuardedSQLService.generate`, settled exactly as for document research;
  5. it applies the read-only guard (`GuardedSQLService.validate`) and policy-to-SQL verification.

  Only then does it issue an `SQLHandle(task_id, handle_id)`. The SQL stays in process.
- `ExecuteSQL(task_id, role, handle)` uses the handle up on its first presentation, whatever
  happens next.
- `PrepareCorrection(task_id, role)` uses the task's one correction on the last failed execution
  the gateway itself recorded, and issues a new handle.

A handle is bound to the tenant, workspace and investigation (the scope), the plan digest, the
authority epoch, the task, the attempt, the role, the resolved table (path, name, columns,
encryption), an HMAC-SHA256 fingerprint of the validated SQL under a per-gateway key, the policy
status, and the approval requirement. It expires after `SQL_HANDLE_TTL` (10 minutes), and never
later than the plan approval deadline.

Before the executor runs, `execute_sql` checks, in order:

| Check | Refusal |
|---|---|
| Handle unknown, altered, issued by another gateway, presented for another task, or presented by another role | `handle_unknown` |
| Handle already presented | `handle_replayed` |
| Handle revoked (task refused or gateway closed) | `handle_stale` |
| Task no longer running, or on another attempt | `handle_stale` |
| Handle expired; scope, plan digest, or epoch changed | `handle_stale` |
| Approval required | `sql_approval_required` |
| Live authority, then the tenant's table re-resolved | as in S5 |
| Table identity changed | `handle_stale` |
| Read-only guard re-applied; fingerprint and policy status recomputed | `handle_stale` |
| One SQL execution reserved | `budget_exhausted` |

The executor is the trusted `execute_sql`, with its table and column allowlist and its row and time
limits, behind a counting wrapper. An execution that never entered the executor is released;
one that did is charged. Cancellation or a concurrent call during execution charges it and keeps
no evidence.

Outcomes:

- **Success**: `query` evidence with `QueryProvenance` (dataset, fingerprint, row and column
  counts, corrections used, policy status, elapsed milliseconds). The rows and SQL stay in
  `evidence_payload`.
- **Guard or timeout refusal by the executor**: the task fails (`unsafe_query` or `timeout`) and is
  never offered a correction.
- **Database error with the correction unused**: the task stays `running`, and the gateway keeps
  the bounded error text for the correction prompt only.
- **Database error after the correction**: the task fails (`sql_generation`), or stays `running`
  when a further attempt is allowed.

The correction count belongs to the task, not the attempt. A second correction is
`correction_limit`, and the task ends `refused`. A correction with no recorded failure is
`attempt_mismatch`. A new attempt cannot begin while a handle is open, and it discards an unused
correction.

**Exactly one statement.** The question graph's `extract_sql` keeps the first statement and skips
any text before the first `SELECT` or `WITH`. Neither is safe for investigations, where a
`SELECT …; DELETE …` answer would run as its first half. The gateway therefore never validates
`extract_sql`'s candidate. `sole_statement` takes the model's whole answer and works as follows:

- it uses the single Markdown SQL fence if there is one, and refuses two or more fences;
- it splits statements with `sqlparse`, so a semicolon inside a string, comment, or quoted
  identifier never splits;
- it ignores empty, comment-only, and terminator-only statements, and refuses any other count than
  exactly one executable statement;
- it strips comments and applies the shared read-only guard to that statement as written.

A write with a sub-select, or prose before the SQL, is therefore refused rather than reinterpreted.
The same rule runs again on the stored SQL before execution, and the correction path issues its
handle through the same code. `extract_sql` and the question graph are unchanged.

**Unknown double-quoted identifiers.** SQLite treats a double-quoted name that matches no column as
a string literal: `SELECT "secret" FROM t` returns the constant `'secret'` for every row, and
`AVG("churn")` returns `0.0`, instead of failing. Before the executor runs, the gateway compiles the
query (`EXPLAIN`, never executed) against an empty in-memory copy of the table's schema, built from
`StoredTable` metadata, with that fallback turned off (`SQLITE_DBCONFIG_DQS_DML`):

- SQLite's own name resolution, including aliases and CTE names, reports `no such column`;
- the failure is an ordinary database error, so the task's one correction can fix it;
- the execution is released, because the executor was never entered;
- it needs no data and no key, so it covers encrypted tables too; `sqlcipher3` cannot turn the
  fallback off on the real connection;
- the shared executor and the question graph keep SQLite's default behaviour.

Policy: with no trusted `PolicyCriteriaSource` criteria, SQL may run only for a task that cites no
evidence (`not_applicable`). A task that cites evidence without trusted criteria cannot run SQL:
the task fails with `policy_refused`. Document-derived criteria are deferred to the first milestone
in which tasks cite evidence (INV-6 or INV-7, INV-5 decision R4); INV-5 plans cite no evidence. With
criteria, only `structurally_verified` SQL gets a handle.

Approval: no INV-10 approval binding exists yet, so any SQL that needs approval is refused before a
model call, and a handle bound to an approval requirement never executes.

SQL text, result rows, row values, and database error text never enter state, decisions, or
results. Decisions carry fixed codes and categories only. Fingerprints are keyed and per-gateway:
they identify SQL within one investigation run, and are not comparable across restarts.

## Bypass enforcement (INV-3 S7)

The gateway is mandatory at three integration boundaries, and each is enforced at runtime, not by
convention:

- **Tool access.** A specialist holds a `SpecialistGateway` (`InvestigationToolGateway.specialist_port()`).
  It has the five tool operations, no ledger, payloads, handle store, or authority, and it cannot
  be modified. Requests are strict, typed, and frozen, and forbid unknown fields. Even a request
  built without validation (`model_construct`) cannot inject SQL, a query, a role, or a counter: the
  gateway uses only the admitted plan and its own records, and refuses request subclasses.
- **Graph registration.** `InvestigationGraphBuilder(gateway)` (`src/orchestration/investigation_graph.py`)
  is the only way to build an investigation graph:
  - `add_step(name, owner, step)` registers a `guarded_node`, and binds a dispatcher to the
    gateway's own ledger;
  - there is no raw `add_node`;
  - `compile()` uses a fresh strict saver and calls `require_guarded_graph`.

  `guarded_node` records the nodes it builds in a private registry. A raw function, a lambda, or a
  function with copied attributes is refused (`graph_not_guarded`), as is a dispatcher bound to
  another ledger. `load_investigation_state` repeats the check before reading a thread.
- **Specialist modules.** The Supervisor and the INV-5 specialists go in `src/investigation/`. The
  INV-5 dispatcher node and run owner are trusted and live in
  `src/orchestration/investigation_execution.py`, and the pure dispatch policy in
  `packages/investigation/dispatch.py` (INV-5 decision R7).
  `tests/test_investigation_boundaries.py` allows them only the contracts, the typed requests, the
  port, and the builder, and for planning only `PlannerPort`, the planning result types, and the
  `PlanningStage` and `PlanningOutcomeCode` enums. It forbids services, executors, retrievers, model clients, storage,
  `BudgetLedger`, authority types, LangGraph, evaluation code, private attributes, and dynamic
  access, and it tests the checker on synthetic violations. Only `investigation_graph.py` may
  construct `StateGraph(InvestigationState)`.

The Supervisor and the proposal-only graph are delivered by INV-4 (ADR 0024 Amendment 2), and the
specialists and dispatcher by INV-5 (Amendment 3), through these interfaces. Behavioural attacks on
each boundary are in `tests/test_investigation_bypass.py`, and on the owner-only operations in
`tests/test_investigation_gateway_owner_ops.py`.

## Execution phase (INV-5)

An approved plan runs sequentially through the tool gateway (ADR 0024 Amendment 3; brief
`docs/agents/briefs/138-inv5-dispatcher-specialists.md`). Everything runs under scripted bounded
adapters; with any production client, every model-executing operation is refused with
`metering_unbounded`.

**Handoff and seeding.** `InvestigationExecutionRun` reads the proposal thread only through
`load_investigation_state` and verifies it against the trusted `InvestigationAuthority`: the
proposal awaits approval, the three digests and the scopes agree, the approval has not expired,
and every task is executable (a `sql_analysis` task for the SQL Analyst or a `document_research`
task for Document Research). It refuses a second execution of the same approval in the same
process; nothing durable prevents re-execution elsewhere (#143/#144). It then builds the execution
ledger and gateway and runs a new strict thread, `<tenant>:<workspace>:<investigation>:execution`:
intake, `planning`, the plan (unbound Supervisor writer), `awaiting_plan_approval`, the approval
copied from the authority, `queued`, every task registered `pending`, and `running`. INV-5 never
constructs an approval or an authority.

**Dispatch.** The `dispatch` step, bound to the execution ledger, takes exactly one action per
step from the pure policy (`packages/investigation/dispatch.py:next_action`), each with one
gateway operation:

| Order | Action | Gateway operation |
|---|---|---|
| first | Check resources | `resource_status` (records only the ledger snapshot) |
| 1 | Stop: the first investigation-severity refusal in state, or owner-observed cancellation | `close`, then closing bookkeeping |
| 2 | The running task: continue its specialist, retry if eligible, otherwise finish | port call, `begin_attempt`, or `finish_attempt` |
| 3 | Skip a pending document task with no index, or a task whose dependency did not succeed | `skip_task` |
| 4 | Start the first pending task, in plan order, whose dependencies all succeeded | `begin_attempt` |
| 5 | Nothing pending or running: finish | `close(completed)` |

At most one task runs. Cancellation is read before every action, and an unreadable token counts as
cancelled. A result that lands after cancellation is refused by the gateway, keeps no evidence,
and still charges what ran. A stop writes the closed ledger snapshot and closes every open task as
`cancelled` (category `cancelled`, `budget_exhausted`, `policy_refused`, or `internal` after a
defect). A node error, a bare
gateway refusal, or the step bound (an explicit LangGraph recursion limit derived from the plan) is
a defect: the owner closes the gateway (`shutdown`) and reports `failed` in process.

**Retries.** Only the dispatcher decides one: `max_attempts == 2`, a last failure in
`sql_generation` or `retrieval_or_grounding`, an open ledger, and a shared task attempt left.
Timeouts, security and policy refusals, provider configuration, and the unknown outcomes
`provider_failure` and `internal` are finished, never retried; the execution gateway enforces the
same set (`retry_categories`).

**Final status.** A defect is `failed`; then the first investigation-severity refusal decides
(`cancelled`, `budget_exhausted`, or `failed`); then owner-observed cancellation; then all tasks
succeeded is `completed`, some `completed_partial`, none `failed`. The terminal record's category is
the first unsuccessful task's category in plan order.

**Specialists.** `SQLAnalyst` runs `begin_attempt → prepare_sql → execute_sql`, and after a failed
execution with the gateway holding the task's one correction, `prepare_correction → execute_sql`.
`DocumentResearcher` runs `begin_attempt → research_documents`. Both hold only the port and keep
tokens and handles in memory.

**Evidence and the interim report.** The gateway builds a typed payload with each evidence record
and verifies it against the record before issuing it: query rows of JSON scalars (at most 500, with
`truncated`) without SQL, and document sources with their complete location, citations, statuses,
and the answer typed as untrusted model output. `compose_interim_report(view, payloads)` re-verifies
every payload and produces a deterministic, content-limited report labelled "interim, unverified,
association only": task rows, verified query cells (at most 50 rows per table), verified citation
locations only, and fixed limitation codes with reviewed wording. A `failed` report carries codes
only. Payloads and the report stay in process; checkpoints and decisions hold identifiers, counts,
and fixed codes only.

## Hypotheses and verdicts (INV-6)

ADR 0024 Amendment 4; brief `docs/agents/briefs/139-inv6-business-investigation.md`.

**Before approval.** The planning call (`inv6-planning-prompt-v2`, question, schema, and titles
only) may return a `schema_version: 2` plan with hypotheses and typed `hypothesis_test` tasks on
behalf of the Business Investigator. `validate_plan` checks those tasks with the Business
Investigator's permission and only when a hypothesis binds them, resolves every test against the
plan's `test_defaults`, and requires real schema fields, empty `evidence_refs`, and a
`min_denominator` of at least the floor (30). The approval digest binds all of it; v1 digests are
unchanged. Planning output is 2,000 tokens per stage and 4,000 in total.

| Contract | Holds |
|---|---|
| `Hypothesis` | ID, untrusted statement, `relationship: "association"`, testability, missing data, tests, context tasks |
| `HypothesisTestSpec` | method, metric (rate or mean), equality filters, period, comparison group, claim (direction, `min_effect`, `min_denominator`) |
| `Verdict` (in process) | verdict, reasons, recomputed observations, verified context citations |
| `VerdictRecord` (state) | verdict, reasons, cited evidence IDs, test task IDs |

**After the tests ran.** On a normal end, one step at a time:

1. `derive_evidence` (owner-only, no charge) recomputes each usable test's observation from its own
   typed query payload and records derived evidence (`rate_difference` or `mean_difference`);
2. the guarded `investigate` step: the Business Investigator proposes verdicts, the owner
   re-validates them, and the `investigator` writer records them;
3. `close(completed)`.

| Evidence | Verdict |
|---|---|
| Every test's effect in the claimed direction reaches `min_effect` (and required context is verified) | `supported_association` |
| Every test usable, and an effect of zero or in the opposite direction | `refuted` |
| No data for the hypothesis | `untestable` (`required_data_absent`) |
| A failed, skipped, or missing test | `untestable` (`test_not_completed`, `evidence_missing`) |
| Truncated, NULL, too-small, wrongly shaped, inconsistent, or conflicting evidence | `untestable` (matching reason) |
| An effect between zero and `min_effect` | `untestable` (`effect_below_threshold`) |

Numbers come only from typed payload counts, never from a model, the plan, or document prose; NULL
is never zero. No verdict is written after a stop or a defect (the report states
`verdicts_not_evaluated`), and verdicts never change the final status. Checkpoints hold verdict codes,
derived-evidence metadata, and IDs only.

## Critique and bounded replanning (INV-7)

ADR 0024 Amendment 5; brief `docs/agents/briefs/140-inv7-critic-replanning.md` (decisions C1 to
C9).

**Topology.** A v2 run reviews each round between its derivations and the verdict step:

```text
dispatch* → derive* → draft_findings → review_findings → critique
   ├─ admitted: open_follow_up_round → replan → register → dispatch* (follow-ups) → derive* → …
   └─ otherwise: investigate (final verdicts) → close(completed) → finish → terminal
```

`draft_findings`, `review_findings`, `open_follow_up_round`, and the registration are `dispatch`
steps, one owner-only gateway operation each. `critique` (writer `critic`) and `replan` (writer
`supervisor`) are guarded steps. At most two critic rounds (0 and 1) and two follow-up rounds (1 and
2) exist: critique *n* reviews the results of round *n*, and round 2's results are evaluated but
not critiqued again (`critique_rounds_exhausted`).

| Component | Does | Never |
|---|---|---|
| Business Investigator draft (gateway, bounded model call) | Drafts transient, typed claims about the verified results | Enters state, checkpoints, or reports |
| Critic review (gateway, bounded model call, one charged critic round) | Proposes at most 3 stratification gaps | Sees rows, numbers, document text, or the draft |
| Critic (`src/investigation/critic.py`) | Recomputes every finding from verified evidence (`critique_round`) | Holds the gateway, a model, the ledger, or authority; creates tasks; writes state |
| Supervisor `FollowUpPlanner` | Builds stratified re-tests from confirmed gaps | Authors text, widens a test, approves, or schedules |
| Run owner | Validates (`validate_critique`, `validate_follow_up_round`), asks the trusted `FollowUpAuthorizer`, writes through guarded steps | Constructs a `FollowUpApproval` |

**Findings** (fixed severity and disposition per code): `numeric_mismatch` and `direction_mismatch`
(a claim against the recomputed observation, tolerance 0.0001), `causal_overreach`,
`document_contradiction` (a claim resting on a verified citation from an approved document task,
against the data), `unsupported_reference` (a fabricated or foreign ID, never stored),
`evidence_not_bound`, `document_evidence_unverified`, `insufficient_evidence`,
`conflicting_evidence`, `incomplete_coverage` (accepted as a limitation; a critique never retries a
task), and `missing_stratification` (a model gap that deterministic checks confirmed: an approved,
completed SQL rate test; a confirmed numeric schema field that is not already a key; not addressed
before).

**Follow-ups.** Only stratified re-tests (`FollowUpTask`, `StratifiedTestSpec`): the target test's
metric, filters, period, and claim unchanged, plus integer bands over the stratification field, run
once by the SQL Analyst. The result is evaluated by direct standardization with baseline weights,
exactly, from the band counts (`standardized_rate_difference`), and becomes an additional required
test of its hypothesis. A round is admitted only after validation, the round and cumulative attempt
limits, and an approval for exactly its task set (`follow_up_digest`) inside the plan approval's
window. The gateway re-verifies the approval when it opens the round and on every operation on a
follow-up task (`follow_up_not_authorized`, task severity). Production has no authorizer, so it
admits nothing until INV-10.

**Failures.** A malformed draft or review is `invalid` and never retried; a provider failure or a
prompt over the input cap is `unavailable` (the latter is never sent and charges nothing). The
deterministic checks that need no draft still run. A late answer after cancellation is discarded;
the `critique` and `replan` steps write nothing once cancellation is observed. Budget exhaustion
during a follow-up ends `budget_exhausted` without verdicts, while the critique records keep each
round's verdict snapshot. The interim report shows critique rows as codes and states every round
that did not fully happen.

## Verified report (INV-8)

ADR 0024 Amendment 6; brief `docs/agents/briefs/141-inv8-verified-reporting.md` (decisions V1 to
V10).

**Topology.** Every execution run that reaches `finish` ends with a verified report:

```text
… investigate (final verdicts) → dispatch: compose_narrative (at most once) → close(completed)
  → finish → compose_report (writer reporter) → verify_report (writer report_verifier) → terminal
```

| Component | Does | Never |
|---|---|---|
| Report body (`packages/investigation/report.py`) | Builds facts from verified derived evidence with exact arithmetic, hypothesis rows from approved typed specs, method recommendations, and mandatory notes; reuses the interim report's rows | Reads a statement, document prose, or a model's number |
| Narrative call (gateway `compose_narrative`, one bounded model call) | Chooses and orders typed claims over listed fact fields and fixed codes (`inv8-narrative-prompt-v1`) | Writes text; sees rows, SQL, documents, or statements; runs twice, retries, or runs for a stopped run |
| Composer (`report_composer.candidate_report`) | Renders the claims from reviewed templates, or composes the deterministic fallback | Makes a model call |
| Verifier (`report_verifier.verify_report`) | Recomputes facts from the source query payloads; checks every row, citation, verdict, finding, limitation, recommendation, claim, and rendered sentence; scans the masked text | Trusts the report or any model |

**Facts and formatting.**
- Exact fractions, displayed half away from zero.
- Proportions and means are shown to 4 decimals. Differences are signed. Percentage points
  (difference × 100) and relative change (difference ÷ baseline × 100, omitted for a zero
  baseline) are shown to 1 decimal.
- A standardized fact names its stratification.
- An unusable test yields no fact; its hypothesis row shows why.

**Claims.** The claim types are `scope`, `fact`, `comparison` (magnitudes of two differences in one
unit, compared exactly), `verdict`, `finding`, `task`, `limitation`, `recommendation`, and
`no_recommendation`. Unknown fields or types fail parsing (`narrative_invalid`). A `causation`
relationship parses and is rejected (`causal_claim`).

**Verification codes.**

| Group | Codes |
|---|---|
| Numbers | `numeric_unsupported`, `numeric_mismatch`, `unsupported_comparison` |
| Citations | `citation_unknown` (absent from this investigation: fabricated or foreign), `citation_stale`, `citation_misattributed`, `scope_mismatch`, `evidence_omitted` |
| Integrity | `causal_claim`, `verdict_mismatch`, `claim_contradiction`, `task_status_misrepresented`, `status_misrepresented`, `finding_mismatch` |
| Completeness | `section_missing`, `hypothesis_omitted`, `finding_omitted`, `limitation_missing`, `limitation_unsupported`, `recommendation_unsupported`, `recommendation_omitted`, `playbook_rule_unknown` |
| Text | `rendered_text_mismatch` |
| Shape | `report_inconsistent`, `schema_invalid` |

The text scan masks data slots and the reviewed disclaimers, then looks for causal or efficacy
wording, digits or number words, and comparatives outside a comparison. It is defence in depth;
the structural checks and template-only text are primary.

**Recommendations** (`inv8-recommendation-rules-v1`) are investigation-method rules only:
- `controlled_evaluation` (labelled unproven);
- `collect_missing_data`;
- `complete_test`;
- `reconcile_document`;
- `review_follow_up`;
- `rerun_investigation`.

Each cites its trigger. With none, the report says so (`no_eligible_recommendation`).

**Status and fallback.**
- `investigation_status` is always the run's terminal status, and `availability` is `complete`,
  `partial`, or `codes_only`.
- A run that is not `completed`/`completed_partial`, a v1 plan, a disabled narrative, no admissible
  model, or no ledger headroom (`BudgetLedger.can_authorize`, checked without charging) makes no
  narrative call.
- A provider failure, a timeout, an over-large prompt, malformed output, a refusal, or a rejected
  narrative gives the deterministic fallback with its reason. A rejected narrative's violation
  codes are kept in the report and in `report_record`.
- The fallback passes the same verification. If it does not, no report is returned (`unavailable`),
  and the run status never changes.

**State.**
- `report_draft` (writer `reporter`) and `report_record` (writer `report_verifier`) are write-once
  and content-free (modes, reasons, codes, counts, SHA-256).
- They may be written only after `finish` and before the terminal record.
- The report itself is returned in process (`ExecutionResult.report`), and only when its digest
  equals the recorded one.
- `STATE_VERSION` stays 1.
