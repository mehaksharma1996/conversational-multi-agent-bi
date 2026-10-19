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
(S7) are not built yet, and nothing below runs in a route or graph yet.

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
   → compose_report → verify_report → terminal status
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
| Critiques | Critic | Append-only |
| Budget usage | Dispatcher, applying gateway ledger snapshots | Cumulative; never reset |
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
matches exactly (`admit_planning_adapter`). Each call reserves its full 6,000/1,000 stage on the
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
provider error messages stay out of results and state. The research payload stays in process
(`evidence_payload`) until INV-5.

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
the task fails with `policy_refused`, because INV-5 has not yet provided document-derived
criteria. With criteria, only `structurally_verified` SQL gets a handle.

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
- **Specialist modules.** INV-4 and INV-5 modules go in `src/investigation/`.
  `tests/test_investigation_boundaries.py` allows them only the contracts, the typed requests, the
  port, and the builder. It forbids services, executors, retrievers, model clients, storage,
  `BudgetLedger`, authority types, LangGraph, evaluation code, private attributes, and dynamic
  access, and it tests the checker on synthetic violations. Only `investigation_graph.py` may
  construct `StateGraph(InvestigationState)`.

Actual Supervisor, specialist, and dispatcher wiring is deferred to INV-4 and INV-5, which must use
these interfaces. Behavioural attacks on each boundary are in `tests/test_investigation_bypass.py`.
