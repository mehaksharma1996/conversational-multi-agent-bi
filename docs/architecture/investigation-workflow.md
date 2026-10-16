# Investigation workflow

How the multi-agent investigation fits beside the existing question graph. The decisions are in
[ADR 0024](../adr/0024-multi-agent-investigation.md), and delivery is tracked by
[#131](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/131). Status
today: only the contracts in `packages/investigation/` exist (INV-0). Nothing below runs yet.

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
| Task status | Dispatcher | Moves forward only: pending → running → terminal |
| Evidence | Specialists, via the gateway | Insert-only; IDs assigned by the server |
| Hypotheses and verdicts | Business Investigation | Verdicts must cite existing evidence |
| Critiques | Critic | Append-only |
| Budget usage | Tool gateway (ledger) | Cumulative; never reset |
| Approvals | API resume path | Typed decisions only |
| Report and terminal status | Composer and terminal nodes | Written once |

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
