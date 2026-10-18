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
profiles. It is not evidence about any settlement.

**Current production providers remain unsupported for model-executing investigation tools.** No
production client is a `BoundedModelAdapter`, so document research and any other model-executing
investigation tool is refused with `metering_unbounded` before any resource is resolved or budget
reserved.

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
