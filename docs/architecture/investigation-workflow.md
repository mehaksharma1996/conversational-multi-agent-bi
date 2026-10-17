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
(`src/orchestration/investigation_checkpoint.py`). The gateway's executing operations (S5 to S7)
are not built yet, and nothing below runs in a route or graph yet.

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
graph is built (`guarded_node(..., ledger=)`). Outside `running`, a change to budget usage needs that
binding and must equal the ledger's snapshot exactly. After a terminal status the ledger must also
be closed, so it cannot authorize another reservation. When a ledger is bound, its snapshot is
required in every status. Binding the right ledger to the right investigation is trusted wiring:
state cannot tell two ledgers apart, and `restore_state` checks only shape and monotonicity. While
`running`, an unbound dispatcher can still write budget usage, so S5 must bind the gateway's ledger.
The settlement itself (closing and settling the ledger at a terminal status) is S5's job.

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
