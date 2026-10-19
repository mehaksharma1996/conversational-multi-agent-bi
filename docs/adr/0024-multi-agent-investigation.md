# ADR 0024: Multi-agent investigation workflow on LangGraph

- Status: Accepted
- Date: 2026-10-15
- Issue: [#133](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/133), under
  umbrella [#131](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/131)
- Builds on [ADR 0017](0017-model-providers-and-fallback.md) (providers and fallback),
  [ADR 0020](0020-in-process-execution-and-job-contract.md) (jobs), and
  [ADR 0022](0022-durable-workspace-metadata.md) (durable metadata).

## Context

The question graph (`src/orchestration/langgraph_orchestrator.py`) answers one question through one
route: memory, SQL, document RAG, hybrid policy-to-SQL, or unsupported. Questions such as "Why did
customer churn increase last quarter, which segments contributed most, and what should management do
about it?" need several dependent steps: quantify the change, decompose it by segment, retrieve
policy evidence, form and test explanations, check the conclusions, and report with stated
uncertainty. The workflow baseline already files such questions as `requires_agent`
(`evals/baseline/v2/investigation_cases.json`).

The approved architecture (October 13, 2026) is a required Iteration 2 deliverable.

## Decisions

1. **Components (all mandatory; none may be silently removed).** A LangGraph intent router; a
   Supervisor/Planner; four specialists (SQL Analyst, Document Research, Business Investigation
   Agent, Critic Agent); typed shared investigation state; bounded feedback and replanning; policy
   guards at every execution boundary; human approval; and a verified investigation report. They
   may ship in stages (#131), but the umbrella closes only when all run in the graph and are evaluated.
2. **LangGraph remains the orchestrator.** Simple questions keep their current routes unchanged. An
   investigation is a dedicated subgraph with its own state schema. No custom agent framework.
3. **The router proposes; a person approves.** The router may select `investigation`, which runs only
   a *proposal phase*: the Supervisor plans under a small enforced budget (2 model calls, 12k input
   and 2k output tokens, 30 seconds, no tools) from the schema and document titles alone, so no rows
   or passages reach a model before approval. Execution starts only after explicit plan approval.
   Investigation routing stays behind a feature flag, off by default, until the full proposal,
   approval, execution, and report path exists (#142).
4. **Background jobs.** Approved investigations run as tenant-scoped background jobs with progress,
   cancellation, and safe terminal states. Cancellation stops further work; a model or SQL call
   already in flight finishes, and its result is discarded. Nothing promises immediate interruption.
5. **Typed requests, deterministic tools.** Agents return validated structured output. They never
   call tools directly: tasks are typed requests, and deterministic code executes them through one
   tool gateway that applies tenant and workspace binding, consent, capability, budget, cancellation,
   and the existing SQL, RAG, and policy-verification guards. No native tool calling is added, so the
   provider fallback chain and the scripted test provider keep working.
6. **Roles and permissions.** Only the SQL Analyst and Document Research execute tasks, through the
   existing guarded services. The Supervisor plans and replans. The Business Investigation Agent
   proposes hypotheses and hypothesis tests. The Critic reads state and may request at most three
   follow-ups per round. It cannot change budgets, approvals, policy flags, or security controls.
   Every proposed task, from any role, passes the same deterministic validator. Text from documents
   or rows never creates tasks.
7. **Bounded execution.** Defaults: 12 task attempts including retries and follow-ups, 2 replan
   rounds, 2 critic rounds, 30 model calls, 150k input and 25k output tokens, 15 SQL executions,
   8 retrievals, 240 seconds. The 12 task attempts include first attempts, retries, and admitted
   follow-ups alike. Plan validation caps how many tasks are admitted; the ledger caps how many
   attempts run, so retries can exhaust the budget during replanning before every admitted task runs.
   The ledger authorizes work before it starts, counting outstanding reservations; settles only the
   reservations it issued; counts every attempt; never resets; and closes permanently on exhaustion,
   an invalid settlement, or a clock fault. Unreported usage is charged at the full reservation and
   flagged as unmetered. That is an upper bound only when the reservation equals a cap the provider
   enforces, which is what the cost classification in decision 11 reports. Exhaustion ends in
   `budget_exhausted` with a partial report.
8. **Sequential first.** Specialist tasks run one at a time. Parallel dispatch, capped at two
   concurrent tasks, comes only after dedicated evaluation and rate-limit testing (#147, #148, #150).
9. **Checkpoints and restart.** Investigation state is JSON-only, so its checkpoints need no pickle.
   Checkpoints are process-local at first. Investigation records and completed reports are stored
   durably. On restart, every non-terminal investigation becomes `interrupted`, with a clear
   user-visible explanation, and nothing resumes silently (as for SQL approvals under ADR 0022).
   Durable checkpoints are a separate design (#149). They require `APP_ENCRYPTION_KEY` and never fall
   back to plaintext.
10. **Approvals.** Plan approval expires after 24 hours, or sooner if the workspace expires. SQL
    approval, when required, is requested once per dispatch wave. A conversation has at most one
    pending approval.
11. **Honest cost.** A cost figure is always labelled as an estimate. It is an
    `enforced_upper_bound` only when every eligible provider, fallbacks included, has known pricing,
    an enforced per-call output cap, input bounded before the call, and metered retries. Otherwise
    it is `indicative`, or `unavailable` when any eligible provider's pricing is unknown.
12. **Verified reporting.** Reports separate supported findings from hypotheses. Observational data
    can support at most an association, never causation. Every number must trace to evidence, and
    every recommendation must cite evidence or a policy passage.
13. **Telemetry and audit stay allowlist-only.** Investigation events carry identifiers, statuses,
    counts, and categories, never questions, SQL, rows, excerpts, prompts, or plans.

## Consequences

- `packages/investigation/` (INV-0) holds the framework-neutral contracts: roles, task kinds, typed
  task inputs, plans, outcomes, evidence references, plan validation, budgets, cost estimates, and
  the lifecycle. Evidence payloads, hypotheses, critiques, and reports are added by the milestones
  that use them (#138 to #141), and each change bumps `SCHEMA_VERSION` when it is not backward compatible.
- The guarded SQL loop and document research must first be extracted from `QuestionOrchestrator`
  without behaviour change (#134), so specialists reuse rather than duplicate them.
- Investigations cost several times more than a simple answer. Claims about quality relative to
  the bounded workflow wait for the live comparison (#147).

## Amendment 1: state, gateway, and bypass enforcement (INV-3, #136, 2026-10-17)

INV-3 makes decisions 5, 7, 9, and 13 concrete and records the limits of what is enforced today.
Details, tests, and refusal codes are in `docs/architecture/investigation-workflow.md`.

1. **Strict checkpoints (refines decision 9).** "JSON-only" means a JSON-native value domain under a
   strict serializer, not JSON bytes on disk; LangGraph stores msgpack.
   - Investigation threads use their own `InMemorySaver`, whose `JsonPlusSerializer` has no pickle
     fallback and no msgpack allowlist, so a checkpoint cannot revive or call arbitrary classes.
   - Restored state is revalidated in full (`restore_state`) before anything trusts it.
   - A reducer that raises poisons a thread, so updates are validated inside the node
     (`owned_update`), and reducers are defence in depth only.
   - Node exceptions become code-only errors, because LangGraph stores `repr(exc)`.
   - An unreadable, poisoned, failed, or unguarded thread fails closed and never resumes.
2. **Trusted authority and consent.** Authority comes only from a trusted adapter
   (`InvestigationAuthority`: scope, capability, consent, plan approval, epoch, provider envelope).
   It is never read from state or agent output; state is only compared with it. Consent is checked
   against the union of the adapter's required recipients and every provider-envelope recipient,
   so an envelope always binds consent. The live re-check runs last, and when it is unavailable the
   operation is refused.
3. **Mandatory gateway execution (decision 5).**
   - Every SQL, document-research, and model call in an investigation runs inside
     `InvestigationToolGateway`. A request carries only a task ID, a role, and a gateway-issued
     token, and is strictly typed with unknown fields forbidden. Queries, SQL, and limits come from
     the admitted plan task; resources come from trusted resolvers.
   - Specialists hold a `SpecialistGateway`: the five tool operations, without the ledger,
     payloads, or internals.
   - Investigation graphs are built only through `InvestigationGraphBuilder`. It registers only
     `guarded_node` steps, binds the dispatcher to the gateway's ledger, and compiles with the strict
     saver. `require_guarded_graph` refuses any unregistered node, at compile time and again before
     a thread is loaded.
   - Specialist modules live in `src/investigation/`. A structural test limits their imports and
     names to the typed requests, the port, the builder, and the contracts; it also forbids private
     attributes and dynamic access.
   - Legacy question-graph modules are unchanged and are not scanned.
4. **Provider envelope (refines decisions 7 and 11).** A model call is admitted only when its client
   is a `BoundedModelAdapter` whose declared bound is the envelope's only member: one attempt, an
   enforced output cap, an exact pre-call input count, and a fixed provider and model. UTF-8 byte
   counts are not used as a token bound. No production client qualifies: Gemini, Anthropic, Ollama,
   fallback chains, and wrappers are refused with `metering_unbounded`. Model-executing
   investigation tools are therefore disabled until a real adapter is reviewed.
5. **Reservation settlement (refines decision 7).**
   - Work is reserved at its worst case before it starts.
   - A reservation whose service provably never ran is released, charging nothing. Proof is an
     invocation marker set before the service is entered, plus a quiet, unfaulted meter; a missing
     report alone is not proof.
   - Unknown usage after an invocation is charged at the full reservation and marked `unverified`.
   - Reported usage above a reservation is charged as reported (never less), recorded as
     `exceeded`, closes the ledger, and fails the investigation (`metering_violation`).
   - `reservation_bound` is `held` only after complete, in-reservation reports, never because an
     adapter was admitted.
6. **Sequential writers (decision 8).** Each channel has one owner. Every guarded write also writes
   a reducer-less channel, so LangGraph refuses a step in which two guarded writers run. A
   concurrent gateway call is investigation-terminal (`concurrent_call`). Parallel dispatch (#150)
   needs its own design.
7. **SQL handles and approval (decision 10).** SQL is prepared and executed in two gateway
   operations.
   - A handle is single-use and expires after 10 minutes, never past the plan approval.
   - It is bound to the scope, plan digest, epoch, task, attempt, role, resolved table, a keyed SQL
     fingerprint, the policy status, and the approval requirement.
   - Exactly one parser-split statement is accepted. Unresolved double-quoted identifiers are
     rejected by a schema-only compile.
   - The gateway counts the one correction a task may use.
   - Until INV-10 provides a statement-level approval binding, any SQL that requires approval is
     refused before a model call, and a handle bound to an approval requirement never executes.
8. **Evaluator-only data.** Investigation runtime modules import no evaluation, test, or script
   code and name no oracle. No gateway request carries a path or resource name. Specialists cannot
   open files.

**Known limitations and deferred milestones.**
- Python cannot hide private attributes, so the specialist rules are structural (AST) on top of the
  runtime facade, builder, and graph check.
- An adapter's exact count, single attempt, and provider-enforced cap are a reviewed code claim;
  the gateway verifies every report afterwards but cannot see an unreported request.
- SQL fingerprints are keyed per gateway and not comparable across restarts.
- Not yet built (the Supervisor and proposal phase were delivered by INV-4; see Amendment 2):
  - the specialists, dispatcher, graph wiring, evidence payload exposure, and document-derived
    policy criteria (INV-5);
  - SQL approval binding (INV-10);
  - routing behind the feature flag (#142);
  - durable checkpoints (#149);
  - parallel dispatch (#150);
  - a real-provider adapter.

## Amendment 2: the proposal phase (INV-4, #137, 2026-10-19)

INV-4 makes decision 3 concrete: a callable, tested, proposal-only subgraph under reviewed scripted
adapters. It also refines decisions 4, 7, 9, and 11 for planning. It does not change execution
(Amendment 1), and investigation routing stays off (#142). Details, codes, and tests are in
`docs/architecture/investigation-workflow.md` and
`docs/agents/briefs/137-inv4-supervisor-planner.md`.

1. **Proposal-only graph mode (decision 2).** `InvestigationGraphBuilder` takes the exact
   `InvestigationPlanningGateway` for proposal mode. That mode registers only guarded `intake`,
   `supervisor`, and `lifecycle` steps, binds the Supervisor writer to the planning gateway's own
   ledger, compiles with the strict saver, and is checked by
   `require_guarded_graph(planning_ledger=...)`. It has no dispatcher, approval, or terminal writer,
   no execution ledger, and no tool gateway. Conversely, an execution graph refuses any
   ledger-bound Supervisor writer, so it never writes a planning record.
2. **Trusted lifecycle ownership.** `InvestigationProposalRun` constructs the planning gateway from
   server-side authority, context, adapter, reviewed evidence, pricing, live check, cancellation,
   and clock; owns its closure; and runs the graph once. The Supervisor
   (`src/investigation/supervisor.py`) receives only `PlannerPort` (`initial()` and `repair()`) and
   never the gateway, ledger, authority, adapter, prompts, or model client. For planning,
   specialist modules may import only the port, the result types, and two pure planning enums.
3. **Planning budget (decisions 3 and 7).** One `BudgetLedger(PLANNING_LIMITS)` per session: at most
   2 model calls, 12,000 input and 2,000 output tokens, 30 seconds, and zero SQL, retrieval, or
   tool calls. Each stage reserves its full 6,000/1,000 partition before the provider-backed exact
   count; the ledger and its original deadline are never reset or extended. The sequence is one
   initial call and, only after a deterministically invalid candidate, at most one repair; every
   other call is a terminal `planning_sequence_violation`.
4. **Planning authority and provider admission.** `PlanningAuthority` carries scope, capability,
   consent, recipients, epoch, and one provider envelope, and has no approval, budget, or price.
   Before each call the gateway re-validates its trusted inputs, checks cancellation and time, then
   scope, `analysis:run`, consent for every recipient, and the live authority last. A model is
   called only through a `DeadlineBoundedModelAdapter` whose single envelope member is covered,
   exactly, by version-matched `ReviewedPlanningEvidence` (`admit_planning_adapter`). No production
   client qualifies, so production planning fails closed with `metering_unbounded`.
5. **Deadline and cancellation (refines decision 4 for planning).** A planning adapter receives the
   ledger's remaining original deadline and must confirm provider-side termination for
   cancellation and deadline outcomes; a local stop without confirmation is `metering_violation`.
   A completed answer after the deadline, a late answer to a cancelled call, or an answer that
   arrives after the gateway was sealed is discarded. Decision 4's "finishes and is discarded"
   remains the rule for execution.
6. **Settlement and metering (Amendment 1, item 5).** The same conservative rules apply, with one
   addition: the reported input must equal the exact pre-call count. Hidden retries, fallbacks,
   another identity, incomplete or excessive reports, meter faults, and provider exceptions are
   charged conservatively and end planning.
7. **Safe results.** `PlanningCallResult` carries only a status (`proposed`, `invalid`,
   `plan_invalid`, `refused`), a fixed code, fixed validation codes with syntactic task IDs, ledger
   usage, and the cost estimate. Planning-only codes are `plan_invalid`, `provider_failure`,
   `planning_sequence_violation`, `planning_abandoned`, and `planning_interrupted`; Supervisor-side
   failures are `supervisor_port_error` and `supervisor_result_invalid`. Raw candidates, prompts,
   provider messages, and exception text never leave the gateway. The repair prompt contains only
   the original metadata (equal `metadata_digest`) and the fixed codes and task IDs.
8. **Model context.** Prompts are rendered deterministically from `PlanningMetadata` only: the
   question, schema field names, logical types, and descriptions, and document display titles. No
   rows, values, passages, SQL, scope identifiers, authority, budgets, prices, or evaluator data.
9. **Proposal record and approval (decisions 3 and 10).** A write-once, Supervisor-owned
   `proposal_record` holds the outcome, a fixed code, the lifecycle target, the plan digest for a
   proposal, the closed planning ledger's `BudgetUsage`, and the `CostEstimate`. It is written with
   the plan in one guarded update, only while `planning`, and must equal the bound planning ledger.
   State invariants tie a proposed record to the stored plan's digest and keep a failed planning
   step at its own target. A proposal reaches only `awaiting_plan_approval` and keeps the
   `plan: required` approval requirement; `queued` and `running` still require a plan approval,
   which the proposal graph cannot write. Withdrawal of a waiting proposal is a later lifecycle
   transition, not a planning operation.
10. **Ledger separation.** The planning ledger is never the execution ledger: a budget write bound
    to a planning ledger is refused, and the planning ledger's identity is fixed when the graph
    compiles. State alone cannot tell two equivalent closed ledgers apart (as in Amendment 1).
11. **Checkpoints (decision 9).** The strict saver and full revalidation are unchanged. The record
    is optional, so `STATE_VERSION` stays 1: pre-INV-4 threads restore unchanged, and an older
    reader refuses a proposal state (`unknown_channel`) rather than misreading it. Checkpoints stay
    process-local; durable checkpoints and their migration rules are reassessed in #149.
12. **Cost classification (decision 11).** `estimate_planning_cost` gives `enforced_upper_bound`
    only when every envelope member has trusted pricing and is covered exactly by reviewed
    evidence; pricing flags and envelope declarations alone give at most `indicative`, and unknown
    pricing is `unavailable`. The figure is the fixed planning limits at the highest eligible
    price, and it never enters the plan or its digest.
13. **Cleanup and terminal outcomes.** Every terminal transition goes through one seal: a refusal,
    `plan_invalid`, or a published proposal is never rewritten, and every later call or close is
    `gateway_closed` (no lifecycle transition). The owner closes the gateway when planning cannot
    continue: `abandoned` for a Supervisor failure or an abandoned repair, `shutdown` on an
    exception, and cancellation, revocation, deadline expiry, or shutdown on request. Closure makes
    no model call and never resets the ledger; a call in flight is sealed and settles its own
    reservation. There is no background worker.
14. **No execution of unapproved plans.** Nothing in INV-4 approves, queues, schedules, or executes
    a plan, constructs an `InvestigationToolGateway`, or reaches SQL, retrieval, or document
    research. Structural tests enforce this for the planning modules, the Supervisor, and
    production code.

**Known limitations and deferred work.**
- A deadline adapter's exact count, single attempt, cap, and provider-side termination remain a
  reviewed code claim; runtime checks verify every report but cannot see an unreported request.
- `PlannerPort`, like `SpecialistGateway`, cannot hide private names from Python; the boundary
  tests enforce that structurally.
- An over-input count closes the ledger `exhausted` without naming `exhausted_resource`, because
  the 6,000-token limit is a stage partition, not a ledger limit; the code carries the cause.
- Missing-information text is in-process only and untrusted; its persistence and display belong to
  the API milestone.
- Not delivered by INV-4: a production deadline adapter and production-owned evidence and pricing
  (a separately reviewed adapter milestone); routing from the question graph (#142); specialist
  execution, dispatcher, and terminal records (INV-5, #138); SQL approval binding (INV-10, #143);
  the investigation API and approval UI (later milestones under #131); durable checkpoints
  (#149); and any production cost guarantee without matching reviewed evidence.

## Alternatives rejected

- **Native tool calling.** It would bypass the single gateway, depend on provider-specific features,
  and break the scripted provider.
- **A custom agent framework.** It would duplicate LangGraph checkpoints, interrupts, and subgraphs.
- **Synchronous message answers.** An investigation makes many model calls and exceeds the request
  budget.
- **A planner followed by single SQL and RAG calls.** This was explicitly ruled out: hypothesis
  testing, critique, and bounded replanning are required.
