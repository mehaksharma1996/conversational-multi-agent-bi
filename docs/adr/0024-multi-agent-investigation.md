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
   and 4k output tokens since Amendment 4, 30 seconds, no tools) from the schema and document titles alone, so no rows
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
   2 model calls, 12,000 input and 4,000 output tokens (2,000 before Amendment 4), 30 seconds, and
   zero SQL, retrieval, or tool calls. Each stage reserves its full 6,000/2,000 partition (6,000/1,000
   before Amendment 4) before the provider-backed exact
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

## Amendment 3: sequential execution (INV-5, #138, 2026-10-19)

INV-5 makes decisions 2, 4, and 5 concrete for execution: an approved plan runs sequentially through
the tool gateway under scripted bounded adapters, and production stays fail-closed
(`metering_unbounded`) until a reviewed bounded adapter exists. Investigation routing stays off
(#142). Details, codes, and tests are in `docs/architecture/investigation-workflow.md` and
`docs/agents/briefs/138-inv5-dispatcher-specialists.md` (decisions R1 to R8).

1. **Re-seeded execution thread (R1).** `InvestigationExecutionRun`
   (`src/orchestration/investigation_execution.py`) is the only code that hands a proposal to
   execution. It reads the proposal thread only through `load_investigation_state` and refuses
   unless the proposal awaits approval with a `proposed` record and no approval in its state, the
   authority's, the record's, and the plan's digests are equal, the scopes are equal, the approval
   has not expired, and every task is a `sql_analysis` task for the SQL Analyst or a
   `document_research` task for Document Research. Execution then runs on a new strict thread
   (`…:execution`), seeded from that verified handoff; the builder and saver are unchanged.
2. **INV-5 grants nothing.** The approval written to the execution thread is copied from the
   trusted `InvestigationAuthority`. No INV-5 module constructs a `PlanApprovalRecord` or an
   `InvestigationAuthority`, or sets `sql_approval_required` to false; a structural test enforces
   this over `src/`, `packages/`, and `apps/`. SQL that needs approval ends its task
   `refused/policy_refused`, and INV-5 never enters `awaiting_sql_approval` (INV-10, #143).
3. **Replay limits.** A run is single-use, and a process-local registry keyed by scope, plan digest,
   and approval time refuses a second execution of the same approval in the same process. Nothing
   durable prevents re-execution in another process or after a restart, and the approval record
   names no approver: durable idempotency, approver identity, and audit belong to #143/#144.
4. **Placement (R7).** The pure dispatch policy is `packages/investigation/dispatch.py`. The
   ledger-bound dispatcher node and the run owner are trusted and live in
   `src/orchestration/investigation_execution.py`. The specialists (`sql_analyst.py`,
   `document_researcher.py`) live in `src/investigation/`, hold only `SpecialistGateway`, and make
   exactly one port call per step.
5. **Owner-only gateway operations.** `resource_status`, `skip_task`, `finish_attempt`,
   `close(completed | cancelled | shutdown)`, and `evidence_payloads` are on the trusted gateway
   and absent from the specialist port. A skip re-verifies its reason (a dependency that ended
   without succeeding, or an absent resource) and charges nothing (R5). `finish_attempt` uses the
   gateway's own record of the attempt's last failure, which must agree with the last decision in
   state. Closing is permanent and idempotent; the first closure wins. The only state vocabulary
   change is the additive `GatewayTool` literal `skip_task`; `STATE_VERSION` stays 1.
6. **Sequential dispatch.** One gateway operation per guarded step; at most one task `running`; a
   task starts only when every dependency `succeeded`, in plan order, so a failure never satisfies
   a dependency. Cancellation is checked before every action, and the step bound is an explicit
   LangGraph recursion limit derived from the plan; reaching it is a defect.
7. **Retries (R6, refined).** The dispatcher, never a specialist, decides a retry. A second attempt
   needs `max_attempts == 2`, a last failure in `sql_generation` or `retrieval_or_grounding`, an
   open ledger, and a shared task attempt left under the limits. Timeouts, security and policy
   refusals, provider configuration, and the unknown outcomes `provider_failure` and `internal`
   are never retried. The execution gateway enforces the same category set
   (`retry_categories`), so even a misbehaving caller cannot retry an unknown outcome. The SQL
   correction is not a retry: it is the gateway-counted single correction of a task.
8. **Final status.** The first match wins: a defect (node error, bare gateway refusal, step bound)
   is `failed`; then the first investigation-severity refusal decides (`cancelled`,
   `budget_exhausted`, or `failed`, including `approval_expired`, because `running → expired` is
   illegal); then owner-observed cancellation is `cancelled`; then all tasks succeeded is
   `completed`, some is `completed_partial`, and none is `failed` (R2). Cancellation, budget
   exhaustion, and security refusals therefore always override the normal rules.
9. **Evidence payloads (R3, refined).** Typed, frozen, process-local payloads
   (`packages/investigation/evidence.py`) are built by the gateway with each evidence record and
   verified against it before the evidence is issued: query rows of JSON scalars (at most 500,
   with an explicit `truncated` flag) and no SQL; document sources with their complete location
   (number, page, chunk, and score must equal the record's provenance), citations, statuses, and
   the answer typed as untrusted model output. Derived evidence is a contract only.
10. **Interim report.** `compose_interim_report` is pure and deterministic and makes no model call.
    Every report is labelled "interim, unverified, association only". It shows verified query cells
    (at most 50 rows per table) and the locations of verified citations only, never answer prose,
    and states limitations as fixed codes with reviewed wording. A `failed` report carries codes
    only (R8). It lives in process; persistence belongs to #144.
11. **Document-derived policy criteria (R4).** Decision 5's document-derived SQL criteria move from
    INV-5 to the first milestone in which tasks cite evidence (INV-6 or INV-7). INV-5 plans cite no
    evidence, so their SQL runs as `not_applicable`; a task that cites evidence without trusted
    criteria still fails `policy_refused`.

**Known limitations and deferred work.** A reviewed real-provider bounded adapter; routing (#142);
approvals, approver identity, durable idempotency, audit, and SQL approval waves (#143); the API,
jobs, and report storage (#144); Business Investigation, hypothesis tests, and derived evidence
(#139); the Critic, replanning, evidence-citing follow-ups, and document-derived policy criteria
(#140); the verified report (#141); observability (#146); evaluation (#145, #147, #148); durable
checkpoints (#149); parallel dispatch (#150); UI (#155, #156); security review (#157); and
deployment (#159).

## Amendment 4: hypotheses, tests, and verdicts (INV-6, #139, 2026-10-19)

INV-6 makes decisions 6 and 12 concrete: the Business Investigation Agent's hypotheses are tested
on approved evidence and receive deterministic, evidence-cited verdicts. Routing stays off (#142),
and production stays fail-closed without a reviewed bounded adapter. Details are in
`docs/architecture/investigation-workflow.md` and
`docs/agents/briefs/139-inv6-business-investigation.md` (decisions D1 to D4).

1. **Plan-embedded hypotheses (D1).** Hypotheses and their typed tests are part of the plan a
   person approves: the plan digest binds every hypothesis and executable test specification
   (fields, filters, periods, metric, direction, thresholds, method, dependencies, and task
   bindings). Hypothesis tests are validated as the Business Investigator's proposals
   (`TASK_PROPOSERS` is unchanged), only when a plan hypothesis binds them; the Supervisor gains no
   authority to create specialist tasks. Nothing is inserted after approval, and admitted follow-ups
   stay unused (INV-7 and #143).
2. **Planning output budget (D2).** Output is 2,000 tokens per stage and 4,000 in total; 2 calls,
   6,000 input per stage and 12,000 in total, 30 seconds, and no tools are unchanged. Exact adapter
   admission requires the new cap, so reviewed capability evidence for the old 1,000-token bound is
   refused. The cost estimate prices the new limits; its classification is unchanged. No reviewed
   provider tokenizer was available: the compact churn response measured 825 to 1,369 tokens by
   proxies (bytes, characters ÷ 3 and ÷ 4, WordPiece), against a 1,800-token gate, and the adapter's
   exact count remains the runtime authority. A live, reviewed measurement is owed before any
   production activation.
3. **Versioned plans (D3).** Hypothesis-bearing plans are `schema_version: 2`
   (`InvestigationPlanV2`). Version-1 canonical bytes and approval digests are unchanged (a pinned
   test). `parse_plan` dispatches on an exact integer version and refuses missing, boolean, float,
   string, and unknown versions. `STATE_VERSION` stays 1: the state additions are optional, every
   earlier checkpoint restores unchanged, and an older reader refuses new vocabulary rather than
   misreading it. The planning prompt is versioned `inv6-planning-prompt-v2`.
4. **Observational verdicts only.** A hypothesis's `relationship` has the single value
   `association`. Verdicts are `supported_association`, `untestable`, or `refuted`, with fixed
   reasons; no value, reason, or report field expresses causation, and statements are untrusted
   display text that never reaches a report.
5. **Deterministic evaluation.** `evaluate_hypothesis` recomputes every observation with exact
   rational arithmetic from integer counts in verified typed query payloads, matching each group
   and period exactly. A test supports when the effect in the claimed direction reaches the approved
   `min_effect`, and refutes when it is zero or opposite. Absent, failed, truncated, NULL, too-small
   (below the configurable floor of 30, a data-sufficiency rule rather than a significance test),
   inconsistent, wrongly shaped, or conflicting evidence makes the hypothesis `untestable`, never
   `refuted`. Required document context counts only through verified citations. Document prose is
   never a measurement or an instruction.
6. **Derived evidence (D4).** The owner-only gateway operation `derive_evidence` recomputes a
   test's observation from the typed payload it issued and records derived evidence: a fixed
   operation (`rate_difference` or `mean_difference`) bound to the test task and its single source
   query evidence. The value and counts stay in process. No model call, no charge, and no caller
   number is accepted.
7. **Business Investigator and verdict writer.** `src/investigation/business_investigator.py` is
   deterministic and holds nothing: it reads what the trusted owner hands it and proposes verdicts.
   The owner re-validates each (`validate_verdict`, and derived evidence must restate the
   observation exactly) and writes content-free `VerdictRecord`s through a new `investigator`
   writer into a write-once `verdicts` channel, during `running`, before the normal close. A
   decisive verdict must cite derived evidence of its own tests. No verdict is written after a stop
   or a defect, and verdicts never change the final status.
8. **Execution integration.** Approved hypothesis tests run through the INV-5 dispatcher and
   specialists, routed by method (SQL methods to the SQL Analyst, `document_check` to Document
   Research). The gateway renders each test's resolved specification into its SQL question; the
   registered SQL prompt builder is unchanged. Retry, cancellation, budget, and terminal semantics
   are unchanged.
9. **Interim report.** Verdict rows carry the hypothesis ID, verdict, reasons, and cited evidence
   IDs only. A v2 run that stopped before evaluation states `verdicts_not_evaluated`.

**Known limitations and deferred work.** The planning token measurement is by proxy only; the v2
planning prompt is versioned and pinned by tests but not yet registered in
`evals/v1/prompt-registry.json` (a reviewed baseline change); thresholds are model-proposed and
human-approved, so the approval UI (#144) must show them; the SQL model may return the wrong shape,
which makes a hypothesis `untestable` (evaluation #145, #147); multi-test hypotheses require every
test to support; the Critic, follow-ups, and document-derived policy criteria are INV-7 (#140); the
verified report is #141.

## Amendment 5: critique and bounded replanning (INV-7, #140, 2026-10-20)

INV-7 makes decisions 6, 7, and 10 concrete for the Critic and the replanning loop. Routing stays
off (#142), and production stays fail-closed: without a reviewed bounded adapter no model call runs,
and without a follow-up authorizer no follow-up runs. Details are in
`docs/architecture/investigation-workflow.md` and `docs/agents/briefs/140-inv7-critic-replanning.md`
(decisions C1 to C9).

1. **Composite Critic (C1).** Deterministic checks over verified evidence decide every finding.
   A bounded model review may only propose coverage gaps (stratification of an approved test), and a
   proposal counts only when deterministic checks confirm it. Severity and disposition are fixed per
   code. The Critic (`src/investigation/critic.py`) holds nothing and writes nothing; the run owner
   re-validates its result (`validate_critique`) before the `critic` writer records it.
2. **Transient claim draft (C2).** Structured state cannot carry a wrong number, a causal claim, or
   a document's assertion (verdicts are recomputed, causation is unrepresentable, and document
   claims exist only as prose), so the Business Investigator's bounded, metered, owner-only gateway
   call drafts typed claims for the Critic to verify. Claims never enter state, checkpoints, or
   reports; a failed draft removes only the claim checks.
3. **Separate approval per follow-up round (C3, refines decision 10).** Follow-ups are outside the
   plan digest, so each round needs a trusted approval bound to its exact task set
   (`follow_up_digest`), its round, and the plan approval's window. It comes only from the
   `FollowUpAuthorizer` port; no production module constructs a `FollowUpApproval` (a structural
   test). Production has no authorizer until INV-10 (#143), so an unauthorized round is the
   limitation `follow_up_not_authorized`, never a silent completion. The gateway re-verifies the
   approval when the round opens and on every operation on a follow-up task.
4. **Stratified re-tests only (C4).** A follow-up copies an approved SQL rate test's metric,
   filters, period, and claim and adds integer bands over a confirmed numeric field. It is evaluated
   by exact direct standardization with baseline weights from the verified band counts, recorded as
   `standardized_rate_difference` derived evidence, and becomes an additional required test of its
   hypothesis. The original evidence is never changed. A critique never retries a task.
5. **Bounded rounds (C5, decision 7).** At most two critic rounds and two follow-up rounds, reserved
   and settled on the execution ledger and never reset; at most three follow-ups per round, within
   the cumulative 12 task attempts. A third round cannot be represented in state, validated, or
   reserved. A gap addressed once is never requested again, so repeated model requests cannot
   extend the loop.
6. **Verdicts (C6).** The write-once `verdicts` channel is written once, after the last round;
   each critique record keeps a content-free verdict snapshot, so reassessment is visible.
   Observational evidence still supports association only.
7. **State (C7).** Additive channels `critiques` (writer `critic`) and `follow_up_rounds`
   (Supervisor); admitted follow-ups are `FollowUpTask`s and must belong to an approved round;
   `GatewayTool` gains `draft_findings`, `review_findings`, and `open_follow_up_round`;
   `DerivedOperationName` gains `standardized_rate_difference`; `RefusalCode` gains
   `follow_up_not_authorized` (task severity). `STATE_VERSION` stays 1: earlier checkpoints restore
   unchanged, and older readers refuse the new vocabulary.
8. **Model context (C9).** The draft prompt (`inv7-findings-prompt-v1`) carries verified
   measurements, verdict codes, approved statements, and verified citations with fenced, untrusted
   answer text. The Critic prompt (`inv7-critic-prompt-v1`) carries test structure, verdict codes,
   numeric field names, and addressed gaps only, so document text can never steer a follow-up.
9. **R4 stays deferred (C8).** Follow-ups carry no `evidence_refs`, so their SQL runs
   `not_applicable`; document-derived policy criteria move to the first milestone that admits
   evidence-citing SQL.

**Known limitations and deferred work.** The authorizer is a synchronous port: the human approval
flow, pausing, approver identity, and audit belong to INV-10 (#143) and the API (#144). The two
review prompts are versioned and pinned by tests but not registered in
`evals/v1/prompt-registry.json`. Coverage gaps are found only when the model proposes them (live
recall is an evaluation question, #145 and #147). Standardization is descriptive and for rate
metrics only. A follow-up that does not complete downgrades its hypothesis to `untestable`.
Checkpoints and replay protection remain process-local. The verified report is INV-8 (#141).

## Amendment 6: the verified report (INV-8, #141, 2026-10-21)

INV-8 makes decision 12 concrete. Every execution run that reaches `finish` ends with a report
accepted by a deterministic verifier. Routing stays off (#142), and production stays fail-closed:
without a reviewed bounded adapter no narrative call runs, so production reports are the
deterministic fallback. Details are in `docs/architecture/investigation-workflow.md` and
`docs/agents/briefs/141-inv8-verified-reporting.md` (decisions V1 to V10 and their review).

1. **One reporting architecture (V1).** `packages/investigation/report.py` extends the INV-5
   interim report: the verified report reuses its verified task, query, citation, critique, and
   limitation rows. `InterimReport` is unchanged.
2. **Deterministic facts (V2).**
   - Every number in a report is a fact built from verified derived evidence with exact
     arithmetic, by one formatter.
   - The verifier recomputes each fact on a separate path (from the source query payload) and
     requires equality.
3. **No model-written text (V3, V4).**
   - The narrative model may only choose and order typed claims of a finite set. Every sentence
     is rendered from a reviewed template plus verified values.
   - The verifier checks each claim's assertions against the evidence, re-renders every sentence
     (exact equality), and scans the masked text for causal, numeric, and comparative wording.
   - The structural controls are primary; the scan is supplementary.
   - Every report states that observational evidence cannot establish causation.
4. **Method recommendations only (V5).**
   - A reviewed, versioned catalogue (`inv8-recommendation-rules-v1`) of six investigation-method
     rules, each fired by a verified trigger and citing it.
   - It is not a business playbook. A rule that touches an intervention is labelled unproven, and
     an unknown rule is `playbook_rule_unknown`.
5. **Descriptions from approved specs (V6).** Hypotheses are described from their approved typed
   tests. Amendment 4 decision 4 stands: statements never reach a report.
6. **One bounded narrative call (V7).**
   - It is an owner-only gateway operation, `compose_narrative`, made at most once, after the
     final verdicts and before the gateway closes.
   - It runs only for a v2 run that would end `completed` or `completed_partial`, and only after a
     no-charge readiness check (an admissible bounded model, and ledger headroom via
     `BudgetLedger.can_authorize`).
   - Its prompt (`inv8-narrative-prompt-v1`) carries fact fields and fixed codes, never rows,
     documents, or statements.
   - No retry and no loop.
7. **Report status (V8).**
   - The report keeps the run's terminal status, and its `availability` is `complete`, `partial`,
     or `codes_only`.
   - Any narrative failure gives the deterministic fallback, with a fixed reason (and a rejected
     narrative's violation codes).
   - A stopped run never gets a narrative or reads as complete. A report defect never changes the
     run status.
8. **Graph and state (V9).**
   - Two guarded steps between `finish` and `terminal`: `compose_report` (writer `reporter`,
     channel `report_draft`) and `verify_report` (writer `report_verifier`, channel
     `report_record`).
   - Both channels are write-once and content-free (modes, codes, counts, SHA-256), and allowed
     only in that window.
   - The report is returned in process and only when its digest matches. No report storage is
     added before INV-11.
   - `GatewayTool` gains `compose_narrative`. `STATE_VERSION` stays 1: earlier checkpoints restore
     unchanged, and older readers refuse the new vocabulary.
9. **Prompt registry (V10).**
   - The registry gains whole-module targets (`package.module`). The INV-7 review prompts
     (`src.orchestration.review_prompts`) and the INV-8 narrative prompt
     (`src.orchestration.report_prompt`) are registered so, and their fingerprints cover their
     complete construction.
   - The INV-6 planning prompt stays unregistered, explicitly listed with its reason. Its rendered
     prompts remain pinned by tests; registering it is a separate reviewed change.
   - The registry file's digest is critical provenance of the v2 workflow baseline. It was
     updated through the documented `--update --justification` procedure: only that digest and
     the review record changed. Every case, outcome, metric, and safety result is identical
     (9/9 measured pass), and no baseline case exercises these prompts.

**Known limitations and deferred work.**
- The narrative's quality and the scan's recall are untested on a real provider (#145, #147).
- `citation_unknown` cannot distinguish a fabricated ID from another investigation's; both are
  rejected.
- The report lives in process only (#144, INV-11).
- The recommendation catalogue is new reviewed content and needs maintainer review like a prompt.
- Registering the INV-6 planning prompt is a follow-up.

## Alternatives rejected

- **Native tool calling.** It would bypass the single gateway, depend on provider-specific features,
  and break the scripted provider.
- **A custom agent framework.** It would duplicate LangGraph checkpoints, interrupts, and subgraphs.
- **Synchronous message answers.** An investigation makes many model calls and exceeds the request
  budget.
- **A planner followed by single SQL and RAG calls.** This was explicitly ruled out: hypothesis
  testing, critique, and bounded replanning are required.
