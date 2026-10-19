# S0 architecture brief: #137 INV-4 Supervisor/Planner proposal phase

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/137

Umbrella: #131. Depends on #136 (INV-3), merged by PR #165 at
`a99dea39384a08b94fc5e62234e1d245df6cfc60`.

Tier: strongest (security and authorization boundary). Check mode: fast
(`python -m scripts.check_all`); no API or web change.

## S0 outcome

This brief defines the Supervisor proposal phase; it does not implement it. The proposed design
keeps `InvestigationPlan` and `validate_plan` as the sole executable-plan contract, runs at most an
initial generation and one repair through a planning-only bounded-model gateway, and uses the
existing `BudgetLedger(PLANNING_LIMITS)` without exposing the ledger or a model client to the
Supervisor. It adds no route, feature flag, API, UI, execution specialist, or production provider.

The design is fail-closed. No production client is currently a `BoundedModelAdapter`, and the
existing bounded contract does not enforce a wall-clock deadline on a call. Until a reviewed
planning adapter supplies both contracts, production planning returns an unavailable/refused
outcome before sending metadata to a model. Tests use a deterministic scripted adapter only.

## Baseline established on 2026-10-18

- `origin/main` was fetched and is exactly
  `a99dea39384a08b94fc5e62234e1d245df6cfc60`; it has not advanced beyond the expected PR #165
  merge. The merge has parents `3f5f8b4` and `b9cb867` and subject
  `Merge pull request #165 from mehaksharma1996/feat/iteration2-inv3-state-gateway`.
- Issue #137 is open, has no closing PR, and has no pre-existing local brief. Its acceptance is the
  scripted churn plan, enforced planning budget, metadata-only input, and honest estimate wording.
- `packages/investigation/contracts.py` defines the only executable-plan shape:
  `InvestigationPlan -> PlanTask ->` one of the three discriminated task inputs. Models are frozen,
  reject extra fields, and are JSON-round-trippable.
- `packages/investigation/validation.py:validate_plan` already checks task IDs, dependencies,
  cycles, maximum depth, proposer and assignee permissions, resource availability, schema fields,
  evidence references, and the task-attempt budget. Violations contain only a fixed code and an
  optional task ID.
- `packages/investigation/budget.py` already fixes planning at 2 model calls, 12,000 input tokens,
  2,000 output tokens, 30 seconds, and zero SQL, retrieval, task, replan, or critic work. The ledger
  reserves before work, counts outstanding reservations, never resets, charges unknown usage at
  the reservation, and closes on exhaustion, clock fault, invalid settlement, or overrun.
- `estimate_cost` is deterministic and uses the highest eligible input and output prices. Any
  unknown eligible price makes the result `unavailable`; any missing enforcement property makes it
  `indicative`; only all-known, all-enforced profiles produce `enforced_upper_bound`.
- INV-3 makes execution authority trusted, checkpoints strict and pickle-free, tool execution
  gateway-only, graph registration builder-only, and specialist imports structurally restricted.
  Rows, passages, SQL, prompts, model output, uploaded filenames, and evaluator oracles are excluded
  from investigation runtime state and gateway results except for the already admitted plan text.
- `src/llm/bounded.py:BoundedModelAdapter` requires a fixed provider/model, one provider attempt,
  exact pre-call input counting, a provider-enforced output cap, and complete usage/identity
  reporting. Gemini, Anthropic, Ollama, fallbacks, and wrappers do not qualify.
- `InvestigationToolGateway` is an execution gateway: it requires an `InvestigationAuthority` that
  contains an approved-plan digest, and its operations require status `running`. It must not be
  reused or weakened to make pre-approval planning fit.
- `InvestigationGraphBuilder` currently accepts exactly an `InvestigationToolGateway`. This is
  correct for execution but cannot construct a proposal graph before a plan or approval exists.
- The INV-2 fixture exposes only `sample_data/subscription_churn.csv` and four PDFs to application
  paths. `evals/investigation/v1/oracle.json` and `reference.sql` are evaluator-only. The primary
  INV-4 case is `churn-full-investigation`: “Why did customer churn increase last quarter, which
  segments contributed most, and what should management do about it?”
- No project skill was invoked. Repository skills are explicit-only, and the S0 request did not
  name one. `docs/agents/toolkit.md` and the brief conventions were reviewed directly.

## Decisions for the proposal design

### D1. One plan contract, with a non-executable proposal wrapper

`InvestigationPlan` remains the only collection of executable tasks. The model returns a
`PlanningDraft` containing exactly:

- `plan: InvestigationPlan`; and
- bounded `missing_information` advisory strings.

The final `InvestigationProposal` wraps that validated plan with deterministic
`approval_requirements`, `CostEstimate`, and content-free planning usage/failure metadata. The
wrapper is not another task or plan schema: only its `plan` can be admitted, digested, approved, or
executed. Cost and approval fields are not accepted from model output.

`approval_requirements` are computed, never generated:

- explicit plan approval is always required;
- SQL approval is labelled `deferred_to_statement_preparation` when the plan contains a SQL-capable
  task, because INV-10 has not yet bound approval to a generated statement; and
- no value can say that consent, authorization, plan approval, or SQL approval has been granted.

Model-provided missing-information text is advisory and untrusted. It is never interpreted as an
instruction, authority, task, field name, approval, or budget change.

### D2. Typed planning metadata is an allowlist

`PlanningInput` is built by a trusted intake adapter and is frozen with `extra="forbid"`. It contains:

- the user question, bounded by characters;
- a tuple of `SchemaFieldMetadata(name, logical_type, description?)`;
- a tuple of bounded display titles (not paths, chunks, passages, or extracted document text);
- `ResourceContext`-equivalent booleans; and
- no tenant/workspace identifiers, provider settings, authorization fields, consent fields,
  budgets, rows, sample values, profiles, aggregates, filenames/paths, passages, evaluation labels,
  oracle values, or reference SQL.

Field descriptions and document titles are untrusted user-controlled data. Prompt rendering uses
canonical JSON in a clearly delimited data section and a fixed system instruction that the section
is data, never instructions. This reduces prompt-injection risk; it does not claim semantic
sanitization. Safety ultimately comes from the output schema, deterministic validation, the absence
of any tool surface, and trusted post-processing.

The metadata adapter must construct `SchemaContext` from the exact same field-name tuple used in
the prompt, and `ResourceContext` from trusted resource presence. The model cannot claim a field or
resource that the validator does not see.

### D3. A dedicated planning gateway, not the execution gateway

Add a trusted `InvestigationPlanningGateway` in `src/orchestration/investigation_planning.py` and a
narrow `PlannerGateway` facade exposed to `src/investigation/supervisor.py`. The facade has only two
stateful operations: initial generation and repair. It exposes no model client, ledger, authority,
tool, resolver, prompt, provider envelope, or private payload.

The trusted gateway owns:

- a planning authority built outside agent/model output;
- one `BudgetLedger(PLANNING_LIMITS)`;
- the admitted planning adapter and fixed provider envelope;
- canonical prompt construction and the prior candidate kept only in process;
- the call-state machine (`new -> initial_used -> repair_used/closed`);
- a non-blocking call lock; and
- content-free results and the deterministic cost estimate.

This is not a bypass around `SpecialistGateway`: that port governs approved execution tools, while
the planning gateway admits no SQL, retrieval, task attempt, resource resolver, or native tool
calling. The zero values in `PLANNING_LIMITS`, the facade shape, import-boundary tests, and the
absence of a tool-capable client enforce zero tool calls.

### D4. Pre-approval authority is separate and trusted

`InvestigationAuthority` cannot authorize proposal generation because it requires the approved
plan that planning is supposed to create. Add a minimal `PlanningAuthority` beside the existing
gateway contracts. It reuses `InvestigationScope`, `ConsentSnapshot`, `ProviderEnvelope`, the
`analysis:run` capability, epoch, and live-authority concepts, but contains no approval record and
no SQL-approval flag.

Before each of the two possible calls, the gateway checks, in order:

1. request type and gateway call state;
2. cancellation and cumulative planning deadline;
3. trusted scope/capability;
4. consent against every eligible provider recipient;
5. live authority/epoch;
6. exact adapter/envelope admission; and
7. the worst-case ledger reservation.

The Supervisor receives none of these inputs and therefore cannot set or override them. A forged
authority or approval field in model JSON is rejected as an extra field before plan validation.

### D5. Existing bounded-provider guarantees plus an enforced deadline

Planning accepts only a reviewed `BoundedPlanningAdapter`, an additive specialization of
`BoundedModelAdapter`. It keeps every existing guarantee and adds a call method that accepts and
enforces a timeout no greater than the gateway's remaining monotonic deadline. A worker-thread
timeout is not sufficient because it does not stop an in-flight provider request.

Admission requires exactly one envelope member, `max_attempts == 1`, a fixed provider/model,
bounded input, and an output cap that matches the adapter. No fallback or wrapper is eligible. No
production adapter currently qualifies, so production fails closed before model invocation. A
scripted adapter in tests implements exact counting, one call, cap enforcement, deadline behavior,
and complete reporting; it is never selectable by production configuration or packaged into a
production image.

The planning totals are partitioned deterministically between the two possible calls:

- initial: at most 6,000 input and 1,000 output tokens;
- repair: at most 6,000 input and 1,000 output tokens.

Each prompt is counted exactly before authorization. A prompt over its partition is refused without
a call. This partition does not weaken the issue limits; it prevents the first call from consuming
the allowance required for the one permitted repair. It is a fixed policy constant derived from
`PLANNING_LIMITS`, never provider- or model-controlled.

The gateway reserves one call, the exact counted input, and the 1,000-token output cap immediately
before each invocation. It sends that cap and the remaining wall-clock allowance to the adapter.
Reports settle through the existing meter/ledger semantics. Missing usage is charged at the full
reservation; an identity mismatch, retry, fallback, incomplete report, overrun, timeout-contract
breach, or meter fault closes the gateway and terminates planning as failed. A completed provider
call after the 30-second cumulative deadline is discarded and classified as timeout/budget
exhaustion; the adapter contract is what prevents spend or data transfer beyond that deadline.

### D6. Validate, then repair exactly once

The Supervisor control flow is deterministic:

1. Ask the facade for the initial candidate.
2. Parse strict JSON as `PlanningDraft`; map parsing/schema failures to fixed, content-free planning
   validation codes. Never expose Pydantic messages or candidate excerpts.
3. Run existing `validate_plan(plan, resources, schema, limits=EXECUTION_LIMITS)`. Planning limits
   govern generation; execution limits govern whether the proposed tasks can later execute.
4. If there are no violations, compute deterministic approval requirements and return the proposal.
5. Otherwise, if and only if the repair call remains authorized, call `repair` with the ordered set
   of fixed codes and optional syntactically valid task IDs. The gateway supplies the prior candidate
   as separately delimited untrusted data; callers cannot supply arbitrary repair text.
6. Parse and validate once more.
7. If still invalid, close the planning ledger and terminate with `plan_invalid`.

Repair never authorizes a third call, resets a ledger, expands a cap, alters metadata, changes
authority, or changes the validation context. Provider/model failures are not repaired: the repair
allowance corrects an invalid plan, not transport failures or hidden provider retries.

The safe planning-validation vocabulary needs fixed codes for failures that occur before
`validate_plan`, such as `malformed_output`, `unsupported_schema_version`, and
`planning_draft_invalid`. Existing `PlanViolation` codes are reused unchanged once a typed plan
exists. Repair prompts contain codes/task IDs only, not schema descriptions, titles, Pydantic error
text, or evaluator facts beyond the original bounded metadata section.

### D7. LangGraph remains the proposal orchestrator

Extend `InvestigationGraphBuilder` with a proposal construction mode rather than constructing a
`StateGraph` elsewhere. The existing execution constructor remains unchanged. Proposal mode takes
the exact trusted planning gateway type, compiles with a fresh strict investigation saver, and
registers only guarded intake, Supervisor, and lifecycle steps. It rejects dispatcher, specialist,
approval, and execution-tool wiring.

Add an optional, write-once, JSON-native `proposal_record` state channel owned by the Supervisor.
It contains only status/code, call counts, `BudgetUsage`, plan digest when valid, and
`CostEstimate`; it contains no prompt, candidate output, missing-information text, validation text,
schema descriptions, or document titles. `guarded_node` binds Supervisor writes of this record to
the planning gateway's ledger exactly as dispatcher budget writes are bound to the execution
gateway's ledger. A valid record and `plan` are written in one validated Supervisor update.

The lifecycle step deterministically maps:

- valid proposal -> `awaiting_plan_approval`;
- two invalid candidates -> `plan_invalid`;
- token/call/wall-clock exhaustion -> `budget_exhausted`; and
- authority, metering, adapter, or model failures -> `failed` with a content-free code.

The proposal graph stops there. It does not approve, queue, execute, route, resume an execution
checkpoint, or construct an `InvestigationToolGateway`. A later approved execution graph uses the
existing builder path and a distinct `BudgetLedger(EXECUTION_LIMITS)`; planning usage never becomes
execution allowance.

Missing-information text is returned in the in-process proposal result for the later API milestone
and is not checkpointed in INV-4. Persistence/display rules belong to INV-9/API work and must treat
it as untrusted model output.

### D8. Cost estimate is deterministic and independent of observed usage

The trusted adapter supplies `ProviderCostProfile` entries for every provider/model eligible for
planning, including any eligible fallback. The model cannot supply profiles or prices. The gateway
calls `estimate_cost(PLANNING_LIMITS, profiles, price_table_version=...)` before generation and
returns the same estimate on success or failure.

- no eligible bounded adapter/profile -> `unavailable(no_eligible_provider)`;
- any eligible unknown price -> `unavailable(pricing_unknown)`;
- known prices but any missing output, input, or retry enforcement -> `indicative` with the exact
  existing limitations; and
- `enforced_upper_bound` only when every eligible path has known prices and all three enforcement
  properties.

`CostEstimate.bound` is an ex-ante pricing classification. It is not replaced by or inferred from
`BudgetUsage.reservation_bound`, which describes observed settlement evidence.

## Proposed module and file structure for S1 onward

| File | Responsibility |
|---|---|
| `packages/investigation/planning.py` | Frozen planning metadata, draft/proposal/result, approval-requirement, safe failure-code, and planning-authority contracts. Reuses `InvestigationPlan`, `BudgetUsage`, and `CostEstimate`. |
| `packages/investigation/state.py` | Add the optional write-once `ProposalRecord` channel and cross-invariants; no prompt or model text. |
| `packages/investigation/gateway.py` | Add pure pre-approval authority checks without weakening `InvestigationAuthority`. |
| `src/llm/bounded.py` | Add the deadline-enforcing `BoundedPlanningAdapter` specialization; no production implementation. |
| `src/orchestration/investigation_planning.py` | Trusted planning gateway, narrow facade, adapter admission, exact counting, reservations, metering, timeout, prompt construction, and deterministic estimate. |
| `src/investigation/supervisor.py` | Parse, validate, repair-once, and assemble the proposal using only the facade and framework-neutral contracts. No tools, raw data, authority, ledger, or model client. |
| `src/orchestration/investigation_graph.py` | Add proposal mode/factory to the existing builder; preserve the execution constructor. |
| `src/orchestration/investigation_checkpoint.py` | Bind proposal-record usage to the trusted planning ledger in guarded Supervisor nodes. |
| `tests/test_investigation_planning_contracts.py` | Metadata/draft/result contract and JSON-boundary tests. |
| `tests/test_investigation_planning_gateway.py` | Admission, authority, budget, timeout, metering, call-state, cost, and scripted-adapter adversarial tests. |
| `tests/test_investigation_supervisor.py` | Churn plan, validation/repair, malformed output, injection, forged fields, dependency/role, and failure semantics. |
| `tests/test_investigation_proposal_graph.py` | Builder-only proposal graph, strict checkpoint, state ownership, lifecycle, and separation from execution budget. |
| `tests/test_investigation_boundaries.py` | Permit only the narrow planning facade in Supervisor modules; keep adapter, ledger, authority, tools, files, dynamic access, and evaluator code banned. |
| `docs/architecture/investigation-workflow.md` and ADR 0024 amendment | Document the reviewed proposal boundary after implementation approval. |

No file above is authorized for implementation by S0; this brief is the only S0 tracked-file
change.

## Failure semantics

| Condition | Model calls | Terminal/status result | Notes |
|---|---:|---|---|
| Initial plan valid | 1 | `awaiting_plan_approval` | Proposal only; no execution. |
| Initial invalid, repaired plan valid | 2 | `awaiting_plan_approval` | Same metadata and validation context. |
| Initial and repaired plans invalid | 2 | `plan_invalid` | Fixed codes only; candidate text discarded. |
| Initial invalid, repair cannot fit remaining token/time budget | 1 | `budget_exhausted` | No second call is attempted. |
| Third call requested or call order repeated | <=2 | `failed` | Gateway closes; call is refused before invocation. |
| Input/output reservation exceeds planning total | <=1 | `budget_exhausted` | Refused before work. |
| Cumulative deadline reached before call | <=1 | `budget_exhausted` | Refused before work. |
| Adapter misses enforced deadline | charged | `failed` or `budget_exhausted` | Result discarded; gateway and ledger close. |
| No eligible bounded planning adapter | 0 | `failed` (`metering_unbounded`) | Production default today. |
| Model/configuration error | charged conservatively if invoked | `failed` | No repair; no provider error text retained. |
| Usage unknown/incomplete | full reservation | `failed` (`metering_violation`) | Never treated as zero or held. |
| Cancellation/authority revocation | 0 or conservative in-flight settlement | `cancelled`/`failed` | Rechecked before each call. |
| Tool request or resolver access | 0 tool calls | refused/test failure | No such facade method or imported capability exists. |

## Acceptance and adversarial test matrix

| Area | Required cases and assertions |
|---|---|
| Churn | A scripted response for `churn-full-investigation` produces a typed plan of bounded SQL and document tasks, valid dependencies, correct assignees, and `validate_plan == ()`; no oracle/reference SQL is loaded or named. |
| Decomposition | Independent tasks can share a level; dependent tasks are topologically valid; task count/depth/attempt totals remain within existing execution limits. |
| Typed output | Valid JSON round-trips; unknown fields, unsupported schema version, wrong discriminators, malformed JSON, oversized strings/collections, and empty plans are repairable fixed-code failures. |
| Repair | One invalid then valid uses exactly two calls; two invalid results end `plan_invalid`; a request for a third call is refused; repair receives only ordered codes/task IDs plus the separately delimited prior candidate. |
| Dependencies | Unknown/self/cyclic/deeper-than-four dependencies are rejected by existing codes; every member of a cycle is reported consistently. |
| Roles | SQL only to SQL Analyst, document only to Document Research, method/assignee rules preserved, and Supervisor-prohibited hypothesis tasks fail until the contract decision below is resolved. |
| Budget calls | Initial-only is one call; repaired is two; hidden retry/fallback/second provider/second report is a metering violation; outstanding reservations count. |
| Tokens | Exact pre-call input count; >6,000 on either prompt refused; output cap is exactly 1,000 per call; cumulative counts never exceed 12,000/2,000; over-report closes and charges the reported amount. |
| Time | Fake monotonic clock covers pre-call exhaustion, remaining-deadline calculation, backward/failing clock, cumulative 30 seconds, adapter timeout, and late-result discard. Tests do not depend on sleeping. |
| Zero tools | `PLANNING_LIMITS` tool resources remain zero; Supervisor AST/import tests ban all tool gateways/services/resolvers/files; scripted adapter records model calls only; injected requests to execute SQL or retrieve documents cannot cause a call. |
| Metadata | Typed intake rejects rows, values, aggregates, passage text, paths, filenames, prompts, provider settings, authority/approval/budget fields, and unknown keys. Canary rows/passages never appear in rendered prompts or gateway results. |
| Injection | Instruction-shaped document titles, field descriptions, question text, and prior invalid output stay in delimited data; they cannot add output fields, change roles, call tools, reveal canaries, or alter validator context. |
| Forgery | Model JSON with consent, scope, provider, budget, `approved=true`, approval digest, or SQL approval fields is rejected by `extra="forbid"`; trusted authority and deterministic approval requirements remain unchanged. |
| Providers | Ordinary Gemini/Anthropic/Ollama/fallback/wrapped clients and a plain `BoundedModelAdapter` are refused; only the scripted deadline-capable adapter is admitted in tests. |
| Usage failures | Missing counts, wrong identity, unknown usage, meter fault, adapter error, cap breach, hidden retry, and fallback are conservatively charged and never produce a proposal. |
| Cost | No provider, unknown price, each individual enforcement limitation, mixed eligible profiles, highest-price selection, and fully enforced profiles produce the exact existing classifications and upward-rounded amount. |
| State/graph | Only `InvestigationGraphBuilder` constructs the graph; strict saver; guarded sequential nodes; proposal record matches the bound planning ledger; plan write once; valid/invalid lifecycle mapping; execution ledger remains separate. |
| Oracle isolation | Runtime modules import no `evals`, `tests`, `scripts`, or `packages.evaluation`; strings name no oracle; the public churn question/schema/title fixture is copied into test builders without reading evaluator-only answers. |
| Regression | Existing INV-0/INV-3 contract, budget, boundary, bypass, checkpoint, gateway, SQL gateway, and question-orchestrator tests remain unchanged and pass. |

## S1-S5 implementation plan

Each stage is independently reviewable and requires fresh approval and a usage checkpoint.

### S1 — Planning contracts and state vocabulary

- Add `packages/investigation/planning.py` with the metadata allowlist, `PlanningDraft`, proposal
  wrapper, computed approval-requirement enums, safe validation/failure codes, and proposal record.
- Add the optional proposal-record state channel, ownership, reducers, JSON restore, and invariants.
- Re-export only public contracts from `packages/investigation/__init__.py`.
- Add contract/state tests. No model, prompt, graph, or runtime call in this stage.

Review gate: resolve R1 and R2 below first; confirm whether the optional state addition keeps schema
version 1 or requires a version bump under the repository's compatibility rule.

### S2 — Fail-closed planning authority and bounded invocation

- Add `PlanningAuthority` pure checks and the deadline-enforcing bounded adapter contract.
- Implement `InvestigationPlanningGateway`/`PlannerGateway`, single-member admission, exact counting,
  6k/1k per-call partitions, cumulative ledger/deadline, metering settlement, call-state machine,
  canonical prompt rendering, and deterministic cost estimation.
- Add the scripted adapter under tests only and exhaustive gateway/adversarial tests.
- Keep every production provider ineligible; do not add a provider implementation or fallback.

Review gate: security review of the deadline guarantee, consent recipients, prompt metadata, meter
settlement, and test-provider production exclusion.

### S3 — Supervisor validate/repair-once component

- Implement `src/investigation/supervisor.py` against only the narrow facade and allowed contracts.
- Strictly parse `PlanningDraft`, normalize failures to fixed codes, invoke existing `validate_plan`,
  repair at most once, and compute the final wrapper fields deterministically.
- Extend structural boundary tests and add the churn, malformed, invalid-twice, injection, role,
  dependency, forged-field, provider-error, and unknown-usage cases.

Review gate: confirm no alternate plan schema, tool path, authority field, sensitive prompt logging,
or evaluator import exists.

### S4 — Proposal-only LangGraph wiring

- Add proposal mode to `InvestigationGraphBuilder`; do not construct `StateGraph` anywhere else.
- Bind guarded proposal-record writes to the planning ledger and compile with the strict saver.
- Wire intake -> Supervisor -> deterministic lifecycle transition, stopping at plan approval or a
  terminal planning status.
- Add graph/checkpoint/lifecycle/separate-ledger tests. Do not connect the router, API, jobs,
  approvals UI, execution dispatcher, or legacy question graph.

Review gate: INV-3 owner/bypass review, especially that execution construction is unchanged and
proposal mode cannot register specialists or execution budget writers.

### S5 — Consolidated acceptance, documentation, and readiness review

- Run focused tests followed by `python -m scripts.check_all`; evaluation and OpenAPI files remain
  byte-identical.
- Update `docs/architecture/investigation-workflow.md` and ADR 0024 with the accepted proposal
  boundary, fail-closed provider status, budget partition, and graph mode.
- Record dependencies and leave investigation routing feature-gated/off.
- Do not add a production provider; hand that work to the separately reviewed adapter milestone.

Review gate: approve or defer connection work. INV-4 ends with a callable/tested proposal subgraph,
not a user-reachable route.

## Dependencies and sequencing

- **INV-5 (#138):** consumes only an approved `InvestigationPlan` and the existing execution
  gateway. It must not read the planning gateway, prior candidates, missing-information text, or
  planning ledger. INV-4 can finish before INV-5, but end-to-end execution cannot.
- **INV-10 / approvals (#143):** statement-level SQL approval remains unavailable. INV-4 may only
  state that SQL approval is deferred; it cannot pre-approve SQL or weaken INV-3's refusal of
  approval-requiring statements.
- **Real-provider adapter:** production proposal generation remains unavailable until a separately
  reviewed adapter proves exact complete-request counting, one attempt, fixed identity, enforced
  output cap, complete reports, and enforceable deadline. Pricing configuration is also required
  for anything stronger than `unavailable`/`indicative`.
- **Routing (#142):** no dependency for local proposal tests. Router/API connection remains later,
  feature-gated, and disabled by default.

## Unresolved decisions and blockers for secure S1

### R1. Existing proposer rules contradict the requested hypothesis-task responsibility

`TASK_PROPOSERS[HYPOTHESIS_TEST]` permits only Business Investigator and Critic, while
`validate_plan` validates the initial plan as proposed by Supervisor. Therefore an INV-4 Supervisor
cannot create a `hypothesis_test` task under the existing contract, even though #137's requested
responsibilities say it should define such tasks.

Recommendation: preserve the existing rule. INV-4 should plan SQL/document evidence collection and
record bounded advisory hypothesis questions in `missing_information`; INV-6's Business
Investigator proposes actual `hypothesis_test` follow-ups after evidence exists. If product intent
instead requires initial hypothesis-test tasks, ADR 0024, `TASK_PROPOSERS`, validators, fixtures,
and threat analysis must be explicitly amended before S1. Do not silently relax the validator.

### R2. Existing plan has no missing-information or approval-requirement fields

Changing `InvestigationPlan` would change plan digests and every downstream contract. The proposed
non-executable wrapper preserves the plan schema and makes approvals deterministic, but the owner
must confirm that this satisfies #137. Model-generated approval claims are not an acceptable
alternative.

### R3. Existing builder cannot create a pre-approval graph

`InvestigationGraphBuilder` requires an execution gateway whose authority requires the plan's
approval. Constructing a dummy approval/gateway would forge authority; constructing `StateGraph`
directly would bypass INV-3. The proposal-mode extension in S4 is required and needs a focused INV-3
security review.

### R4. Existing bounded adapter does not enforce a wall-clock deadline

Checking a monotonic clock before and after a blocking call detects but does not enforce the
30-second limit. A thread future cannot cancel an HTTP request reliably. The additive planning
adapter deadline contract is required; until one exists, fail closed. No production provider may be
grandfathered merely because it usually has a timeout.

### R5. Pricing source for eligible planning providers is not yet defined

`LLMPricing` is optional and does not itself enumerate all eligible fallbacks or prove enforcement.
The trusted provider adapter/configuration must supply versioned `ProviderCostProfile` entries. No
price may be inferred from model output, observed billing, or a hard-coded marketing price.

None of R1-R5 justifies weakening authorization, validation, budget, checkpoint, provider, or
oracle-isolation boundaries. R1 and R2 require owner decisions before S1; R3-R5 have the fail-closed
designs above but require implementation review.

## Do not touch in INV-4

`apps/**`, `apps/web/**`, `evals/**`, baselines, OpenAPI, `src/orchestration/langgraph_orchestrator.py`,
legacy question routes/state, existing guarded SQL/document services, production provider clients,
frontend code, audit/telemetry allowlists, dependency/lock files, containers, INV-2 fixtures, and
completed INV-3 worktree/files except the small reviewed additive seams named for S1-S4. Do not
route, approve, execute, commit, push, open a PR, or modify GitHub as part of S0.

## S0 acceptance

- [x] Current `origin/main` and PR #165 merge commit verified.
- [x] Issue #137, ADR 0024, investigation architecture, INV-0/INV-3 contracts, validators, budgets,
      gateway/provider boundaries, relevant tests, churn fixture, oracle isolation, instructions,
      and skills reviewed.
- [x] Module structure, contracts, metadata boundary, budget/model enforcement, repair flow, failure
      semantics, cost integration, INV-3 compatibility, test matrix, staged plan, dependencies, and
      blockers documented.
- [x] No Supervisor/runtime implementation, route, API, UI, evaluation, baseline, or production
      provider change made.

## S2 architecture and contract review: bounded planning gateway

Review date: 2026-10-18. This section refines the S0 design after S1 established
`PlanningContext`, `PlanningDraft`, `PlanningValidationResult`, and `InvestigationProposal`. It is
architecture only: no gateway, adapter, prompt renderer, state, test, provider, or graph code is
implemented by this review.

### S2 baseline and reviewed evidence

- The clean worktree is `work/agent-worktrees/137-inv4-supervisor-planner` on
  `feat/iteration2-inv4-supervisor-planner` at
  `9c152e4ef73926768f260a5ba16cb888de898f31`, tracking the same remote commit.
- Issue #137 remains open with no closing PR. It requires at most 2 model calls, 12,000 input
  tokens, 2,000 output tokens, 30 seconds, and zero tools; the model sees schema information and
  document titles, not rows or passages.
- ADR 0024 and its INV-3 amendment, the investigation architecture, `BudgetLedger`,
  `PLANNING_LIMITS`, `ProviderEnvelope`, `InvestigationAuthority`, `BoundedModelAdapter`, provider
  call metering, cost estimation, S1 planning contracts, and the INV-3 gateway/budget/metering tests
  were reviewed. The test-only `ExactAdapter` demonstrates the existing single-attempt count/cap/
  identity contract, but has no deadline guarantee and is not a production adapter.
- S1 did not change `InvestigationPlan`, `validate_plan`, `plan_digest`, proposer permissions,
  execution authority, the question graph, evaluations, or baselines. In particular, a Supervisor
  still cannot propose `hypothesis_test` tasks.

### S2 conclusions and conflicts with the earlier sketch

The following refinements are required. They are called out rather than silently changing INV-3
or the S1 contracts.

1. **Reserve before counting.** S0 D5 said to reserve the exact counted input. The existing secure
   model wrapper meters `count_input_tokens` because an adapter that violates its declaration could
   contact a provider while counting. Planning must therefore reserve the full per-attempt
   partition before calling the counter, then settle the actual complete report. A quiet counter
   failure proves no invocation and permits release; provider activity or a meter fault does not.
2. **Do not replay the previous candidate.** S0 D6 allowed the prior candidate in a delimited repair
   section. S2 recommends discarding raw provider output after strict parsing and validation. The
   repair prompt receives the original metadata plus fixed validation codes and syntactically valid
   task IDs only. This is less helpful to the model but removes a second injection/content channel.
3. **Add a public deadline checkpoint.** `BudgetLedger.authorize` samples elapsed monotonic time,
   but settlement does not refresh it and the ledger exposes no remaining-time operation. S2 needs
   an additive, lock-protected `remaining_wall_clock()` (name subject to implementation review)
   that uses the ledger's existing clock, closes on a clock fault or exhaustion, and never reserves
   work. Duplicating the ledger start time in the gateway would create two clocks of record.
4. **Deadline admission is stronger than `BoundedModelAdapter`.** A local future/thread timeout can
   stop waiting but cannot prove that a remote request stopped or stopped billing. Planning needs a
   reviewed deadline-capable specialization. No current Gemini, Anthropic, Ollama, fallback, or
   wrapper client qualifies.
5. **ADR 0024 cancellation remains honest.** ADR 0024 says an ordinary in-flight operation may
   finish and be discarded. S2 does not relabel that behavior as cancellation. A planning adapter
   is eligible only if it can impose the remaining deadline on the provider request and can confirm
   termination before returning a cancellation/deadline result. Otherwise production planning is
   disabled with `metering_unbounded`.

### Proposed modules and public interfaces

| Proposed seam | Responsibility and visibility |
|---|---|
| `packages/investigation/gateway.py:PlanningAuthority` | Additive frozen authority containing `InvestigationScope`, `analysis:run`, `ConsentSnapshot`, required recipients, epoch, and one `ProviderEnvelope`. It has no plan approval, SQL approval, budget, price, prompt, or model-generated field. |
| `packages/investigation/gateway.py:check_planning_authority` | Pure checks that reuse INV-3 capability, consent-recipient union, and live-authority semantics without weakening `check_authority`. It additionally binds `PlanningContext.scope` to the authority. |
| `packages/investigation/budget.py:remaining_wall_clock` | Additive sampling operation on the existing planning ledger. It returns the remaining seconds or closes/raises using existing clock-fault and exhaustion semantics. |
| `src/llm/bounded.py:DeadlineBoundedModelAdapter` | Specialization of `BoundedModelAdapter` for exactly one provider attempt, exact complete-request counting, enforced output cap, complete reporting, and provider-side deadline/termination behavior. |
| `src/orchestration/investigation_planning.py:InvestigationPlanningGateway` | Trusted implementation owning authority, context, adapter, `BudgetLedger(PLANNING_LIMITS)`, clock, cancellation, prompt renderer, meter, private prior-call state, pricing profiles, and terminal close state. |
| `src/orchestration/investigation_planning.py:PlannerGateway` | Narrow Supervisor-facing protocol: `initial()` and `repair(errors)`. It exposes no authority, scope mutation, ledger, adapter, prompt, raw response, provider envelope, resolver, SQL/RAG operation, or arbitrary tool method. |
| `src/orchestration/investigation_planning.py:PlanningCallResult` | Frozen tagged result containing either a validated `PlanningDraft`, fixed `PlanningValidationError`s, or a content-free gateway code, plus the trusted `CostEstimate` and ledger `BudgetUsage`. Raw provider text and exceptions are absent. |
| `src/orchestration/planning_prompt.py` | Pure deterministic renderer with no provider, file, database, retrieval, evaluation, authority, or budget import. It accepts only S1 `PlanningMetadata` and, for repair, fixed errors. |
| `tests/investigation_planning_fakes.py` | Test-only scripted deadline adapter and fake clock/cancellation/authority ports. Production modules and images must not import or select it. |

`InvestigationPlanningGateway` is deliberately separate from `InvestigationToolGateway`. The latter
requires an approved-plan digest and `running` state and exposes execution operations. Reusing it
before plan approval would either forge authority or weaken INV-3. The planning facade has no tool
surface, and
`PLANNING_LIMITS` independently fixes SQL, retrieval, task attempts, replan rounds, and critic
rounds at zero.

### Trusted construction and authority checks

The API/intake adapter eventually constructs `PlanningAuthority` and `PlanningContext`; neither is
accepted from a Supervisor or model. The gateway constructor rejects them unless their scopes are
identical. The model projection remains exactly `context.model_metadata`; tenant, workspace,
investigation, dataset, and collection identifiers stay server-side.

Before each possible model call, the gateway checks in this order:

1. the non-reentrant call lock, gateway state, and exact operation (`initial` then optional
   `repair` only);
2. cancellation and remaining planning time;
3. context-to-authority scope equality and the `analysis:run` capability;
4. consent for the union of trusted required recipients and the envelope recipient;
5. the live authority epoch/status, last among authority checks;
6. one-member provider-envelope and deadline-adapter admission;
7. deterministic prompt rendering and the fixed stage partition;
8. worst-case ledger reservation; and
9. exact complete-request counting inside the reserved, metered invocation wrapper.

A failure closes the gateway when it is an authority, cancellation, budget, deadline, concurrency,
adapter, meter, or call-sequence violation. No later call is then possible. A syntactically or
semantically invalid candidate is not an authority failure: it permits the one repair while the
same gateway, authority, context, adapter, ledger, deadline, and cost profile remain bound.

### Deadline-capable adapter contract

The additive adapter should accept the gateway's **remaining monotonic duration**, not an
independently interpreted wall-clock timestamp. Its reviewed implementation must:

- send a provider/server-enforced request deadline no greater than that duration;
- make exactly one provider request, with SDK and transport retries disabled;
- enforce the supplied output-token cap;
- observe cancellation during the call through an adapter-owned cancellable transport;
- not return a timeout/cancelled outcome until provider termination is confirmed;
- report complete token usage, provider/model identity, retry count zero, and a typed termination
  disposition; and
- make no background, telemetry, token-count, retry, or cleanup request outside the admitted
  invocation.

The exact method/result names are an implementation detail, but the result must distinguish
`completed`, `provider_cancelled`, and `provider_deadline_enforced`. A local wait timeout or a sent
cancel request without confirmation is neither of the latter two. Runtime checks can compare the
elapsed clock, response identity, reports, cap, retries, fallbacks, and disposition. They cannot
independently observe an unreported network request or prove provider billing stopped; those facts
remain reviewed adapter-implementation guarantees.

If cancellation or the deadline occurs during a call, the gateway immediately seals itself against
subsequent calls. When the adapter returns:

- complete in-bound reports are settled as reported;
- incomplete or missing reports after invocation charge the full reservation;
- reported overruns are charged as reported and mark the bound `exceeded`;
- an unconfirmed termination is a metering violation, regardless of a local timeout; and
- every response/candidate is discarded.

If the 30-second deadline expires after the initial result but before repair authorization, the
ledger closes as exhausted and repair is refused without rendering, counting, or invoking it.

### Exact planning budget and call state machine

`BudgetLedger(PLANNING_LIMITS)` remains the only resource ledger and is never reset. The fixed
partitions are:

| Stage | Calls reserved | Input reserved | Output reserved | Invocation |
|---|---:|---:|---:|---|
| Initial | 1 | 6,000 | 1,000 | exactly once |
| Repair | 1 | 6,000 | 1,000 | at most once, only after invalid initial output |

Each reservation covers the complete **stage** allowance, not the entire remaining 12k/2k global
budget. This prevents the initial attempt from consuming the repair partition. Counting happens
after reservation and before provider invocation. A non-integer or non-positive count is
`metering_unbounded`; a valid count above 6,000 releases a quiet, unused reservation and closes the
ledger as input-budget exhaustion. The adapter receives exactly `max_output_tokens=1000`.
Settlement may charge less when exactly one complete report proves the lower usage. Unknown usage
charges the partition, never zero.

The state machine is monotonic and internal:

```text
new --initial reserved/invoked--> initial_in_flight
  | success+valid -------------------------------> complete
  | success+invalid -----------------------------> repair_available
  | refusal/failure/cancel/deadline --------------> closed

repair_available --repair reserved/invoked-------> repair_in_flight
  | success+valid -------------------------------> complete
  | success+invalid -----------------------------> plan_invalid
  | refusal/failure/cancel/deadline --------------> closed
```

Only `repair_available` admits repair. A repeated initial, repair-before-invalid, second repair, or
third call is a terminal sequence violation before reservation. Every terminal state closes the
ledger (`completed` only after all reservations settle); outstanding work may still settle after a
close, as INV-3 already requires.

### Metering and provider admission

Admission requires all of the following; one failure yields `metering_unbounded` before model
invocation:

- the client is the deadline-capable subtype, not merely `BoundedModelAdapter`;
- the envelope contains exactly `adapter.bound` and no fallback member;
- provider/model identity matches the bound exactly;
- `max_attempts == 1`, `input_bounded is True`, and output cap is exactly 1,000;
- the deadline guarantee and termination dispositions are implemented by the reviewed adapter;
- the counter covers the complete provider request and performs no billable call; and
- production dependency construction cannot import/select the scripted adapter.

Reuse INV-3's provider meter and settlement rules. Exactly one usage report, one matching
`served_by`, zero retries, zero fallbacks, a response with matching identity, no meter fault, and
reported counts within the exact count/output cap are required for a verified settlement. A second
report, hidden retry, fallback, wrong identity, provider activity outside the wrapper, incomplete
success report, cap breach, or meter fault discards the candidate and closes the gateway. Runtime
metering is defense in depth; it does not turn an unreviewed client into an eligible adapter.

### Deterministic prompt and repair boundary

The renderer uses a fixed versioned instruction and canonical JSON (`sort_keys=True`, compact
separators, UTF-8) in a length-delimited data section. JSON quoting is structural, not a claim that
the text is safe instruction content. The output schema and deterministic validation remain the
security boundary.

The initial model-visible material is limited to:

- bounded question, schema field names/logical types/descriptions, and document display titles;
- the fixed role/task vocabulary, explicitly excluding Supervisor-authored `hypothesis_test`;
- the frozen `InvestigationPlan`/`PlanningDraft` output shape; and
- fixed planning instructions and schema version.

It excludes rows, values, aggregates, passages, document bodies/paths, SQL/results, secrets,
authority/consent, scope/resource identifiers, budgets, provider configuration, prices, raw
messages, logs, evaluator code, oracle data, and reference SQL.

For repair, the renderer repeats the same trusted metadata and adds only the ordered, de-duplicated
fixed validation codes and pattern-validated task IDs. Raw parser/Pydantic/provider exceptions and
the previous provider output are discarded and never re-rendered. Repair output passes the same
strict S1 parsing and `validate_plan(..., limits=EXECUTION_LIMITS)` boundary. The model cannot set
limits, scope, authority, approval requirements, cost, call state, or execution permission.

The gateway accepts a response only through a strict JSON-object decoder: no Markdown-fence or
substring extraction, trailing text, duplicate object keys, non-finite constants, or non-object
root. It then calls the existing `validate_planning_draft`; it never duplicates plan validation or
returns the raw response through the facade.

### Cost-estimation integration

A trusted configuration adapter supplies a versioned tuple of `ProviderCostProfile` values. For S2
the tuple must correspond exactly to the admitted provider/model; there is no fallback. Profiles
are not accepted from the model, authority, prompt, or observed billing. The gateway calls
`estimate_cost(PLANNING_LIMITS, profiles, price_table_version=...)` locally before generation; this
uses no provider and consumes no model budget.

- no admitted adapter/profile: `unavailable(no_eligible_provider)`;
- missing input or output price: `unavailable(pricing_unknown)`;
- known prices but any missing cap/input/retry enforcement: `indicative` with existing limitations;
- `enforced_upper_bound` only when every eligible path has known prices and all existing
  enforcement flags are true.

Deadline enforcement does not create a new cost classification by itself. Conversely, a trusted
price does not make an unbounded adapter admissible. The estimate is computed from the fixed total
planning limits, not observed usage, and remains labelled `kind="estimate"`.

### Safe result and refusal taxonomy

Reuse existing `RefusalCode`s where their meaning is exact:

| Condition | Code/result | Ledger close | External status |
|---|---|---|---|
| malformed facade request | `invalid_request` | no, if no security ambiguity | caller error |
| scope/capability/consent/live authority failure | existing matching refusal | `policy_refused` | `failed` |
| cancellation | `cancelled` | `cancelled` | `cancelled` |
| time/token/call exhaustion | `budget_exhausted` | `exhausted` | `budget_exhausted` |
| adapter/deadline guarantee absent | `metering_unbounded` | `policy_refused` | `failed` |
| report/identity/retry/fallback/deadline violation | `metering_violation` | `reservation_exceeded` or `policy_refused` after settlement | `failed` |
| gateway already terminal | `gateway_closed` | unchanged | unchanged terminal status |
| typed candidate invalid | `PlanningValidationError` tuple | remains open only for first candidate | repair eligible |
| repaired candidate invalid | `plan_invalid` result | `completed` after settlements | `plan_invalid` |
| provider exception after invocation | additive `provider_failure` result | conservative settlement then `policy_refused` | `failed` |
| illegal initial/repair sequence | additive `planning_sequence_violation` | `policy_refused` | `failed` |

The two additive names belong to a planning-result vocabulary, not the execution-tool refusal enum,
unless implementation review demonstrates a safe shared meaning. Messages, prompts, candidates,
provider errors, schema descriptions, and titles never appear in result errors, state, telemetry,
or audit. No S2 telemetry/audit field is required; any later addition must use the existing
allowlist and content-free values.

### Adversarial S2 test matrix

`Call?` means whether a provider invocation may occur. Every case also asserts zero SQL, retrieval,
native-tool, file, or resolver activity.

| Case | Expected result/refusal | Ledger behavior | Call? |
|---|---|---|---:|
| Valid initial draft | validated draft | one verified settlement; close completed | 1 |
| Invalid initial, valid repair | validated repaired draft | two cumulative settlements; close completed | 2 |
| Invalid initial and repair | `plan_invalid` | two settlements; close completed | 2 |
| Repeated initial, repair first, second repair, or third call | `planning_sequence_violation` | close policy-refused; no new reservation | 0 additional |
| Initial/repair rendered input over 6,000 | `budget_exhausted` | release if counter quiet; close exhausted | 0 |
| Cumulative input/output would exceed 12,000/2,000 | `budget_exhausted` | ledger closes exhausted before invocation | 0 additional |
| Deadline already expired | `budget_exhausted` | closes exhausted | 0 |
| Deadline expires after initial/before repair | `budget_exhausted` | first settled; close before repair | 1 |
| Deadline during call, provider confirms enforcement | budget exhausted; candidate discarded | settle complete report or full partition if incomplete; close | 1 |
| Local timeout/cancel sent without stop confirmation | `metering_violation` | full/greater reported charge; close | 1 |
| Cancellation before call | `cancelled` | close; no reservation | 0 |
| Cancellation during call with confirmed provider stop | `cancelled` | conservative settlement; close; discard late data | 1 |
| Late response after gateway sealed | ignored/`gateway_closed` | no second settlement or call | 1 total |
| Hidden retry, second report, or fallback | `metering_violation` | charge reported/full; close, overrun marked if applicable | 1+ observed |
| Incomplete/missing usage after invocation | provider failure or metering violation | full partition, unverified; close | 1 |
| Forged/excessive usage | `metering_violation` | malformed charges full; overrun charges reported and marks exceeded | 1 |
| Wrong provider/model in bound, report, or response | `metering_unbounded` pre-call or `metering_violation` post-call | none pre-call; conservative settlement post-call | 0 or 1 |
| Counter raises quietly | `metering_unbounded` | release reservation, then close | 0 |
| Counter returns non-integer or non-positive value | `metering_unbounded` | release if meter quiet, then close | 0 |
| Counter reports/provider activity | `metering_violation` | full/greater charge; close | observed violation |
| Meter fault | `metering_violation` | full partition, unverified; close | 0 or 1 |
| Ordinary production client or plain bounded adapter | `metering_unbounded` | no reservation | 0 |
| Provider exception | `provider_failure` | full partition unless provably not invoked; close | 0 or 1 |
| Injection in question/title/description | normal validation only | unaffected accounting; rendered as data | as normal |
| Row/passage/secret/oracle/provider message in metadata | S1 schema rejection | no reservation | 0 |
| SQL/RAG/native-tool request | no facade method / `invalid_request` | no reservation; tool counters stay zero | 0 |
| Forged authority/approval/scope/budget in candidate | `planning_draft_invalid` | first may permit repair; second `plan_invalid` | at most 2 |
| Missing pricing profile | valid `unavailable` estimate | no model charge for estimation | as normal |
| Unknown price | `unavailable(pricing_unknown)` | no model charge for estimation | as normal |
| Unenforced cost prerequisite | `indicative` with exact existing limitation | no model charge for estimation | as normal |
| Fully priced/enforced profile | `enforced_upper_bound` | amount uses fixed limits/highest eligible price | as normal |

Tests use a fake monotonic clock and a scripted deadline adapter; they never sleep or access a
network. Structural tests ensure production modules cannot import the fake, execution gateway,
guarded SQL/RAG services, files, evaluation packages, or dynamic/private access. Regression gates
retain all S1, INV-0/INV-3, question-orchestrator, evaluation-baseline, and package-boundary tests.

### Proposed small S2 implementation stages and gates

1. **S2a — Pure authority/deadline contracts.** Add `PlanningAuthority`, its pure checker, the
   deadline-adapter abstract contract, planning result codes, and the ledger remaining-time seam.
   Gate: contract tests for strict fields, scope/capability/consent/live checks, monotonic faults,
   and proof that existing execution authority behavior is byte-for-byte unchanged.
2. **S2b — Renderer and cost inputs.** Add the pure canonical renderer and trusted pricing-profile
   binding. Gate: golden prompt-shape tests, canary exclusion/injection tests, deterministic output,
   and the existing cost-classification matrix using `PLANNING_LIMITS`.
3. **S2c — Single-call admission and settlement.** Add the gateway shell, fixed initial partition,
   adapter admission, meter, conservative settlement, cancellation/deadline handling, and safe
   typed result. Gate: no repair yet; every pre/post-invocation failure in the matrix passes.
4. **S2d — One repair and terminal state machine.** Add only the second fixed partition and
   code/task-ID repair prompt; discard the prior raw output. Gate: valid-first, invalid-then-valid,
   invalid-twice, every illegal sequence, cumulative budget, and between-call deadline tests.
5. **S2e — Boundary and regression review.** Add production-exclusion/import tests and run focused
   suites followed by `python -m scripts.check_all`. Gate: production remains fail-closed, no
   provider client is added, and evaluations/baselines/legacy workflows remain unchanged.

S3 Supervisor work and S4 proposal-graph/checkpoint wiring remain separately authorized future
stages. Nothing in S2 may register a graph, approve a plan, execute a task, or expose a route.

### Unsupported-provider behavior and remaining approvals

Production construction must return a typed unavailable/refused planning result before prompt
rendering or metadata disclosure when no deadline-capable adapter is configured. It must not fall
back to Gemini, Anthropic, Ollama, `FallbackLLMClient`, a wrapper, or a plain
`BoundedModelAdapter`. The scripted adapter is test-only and cannot be selected by configuration.

The following decisions require approval before implementation:

1. Approve full 6k/1k stage reservation before counting, replacing S0's exact-input reservation
   sequence while retaining exact settlement.
2. Approve excluding the previous raw candidate from repair, replacing S0's delimited replay.
3. Approve the additive `BudgetLedger` remaining-time/checkpoint method rather than a duplicate
   gateway clock.
4. Approve the precise provider-termination evidence required by the deadline adapter. Until a
   real implementation is reviewed, only the scripted test adapter qualifies.
5. Identify the trusted owner and version format for provider pricing profiles. Without it, cost is
   `unavailable`; no price is hard-coded or inferred.
6. Confirm that planning-specific provider/sequence failures remain a separate content-free result
   vocabulary until S3/S4 lifecycle mapping is designed, instead of expanding INV-3's execution
   refusal enum prematurely.

No conflict requires weakening ADR 0024, INV-3 authorization, S1 validation, cost honesty, or the
Supervisor prohibition on `hypothesis_test`. The only architectural tension is ADR 0024's honest
statement that ordinary in-flight calls may finish: S2 resolves it by admitting no planning adapter
unless provider-side deadline/termination behavior is a reviewed guarantee.

## S2e integration-readiness audit

Audit date: 2026-10-19, on `feat/iteration2-inv4-supervisor-planner` after S2a-S2d (`9d60c86`) plus
the S2e changes. S2 delivers the bounded planning gateway; it is not yet the Supervisor component
(S3) or the proposal subgraph (S4) that this brief names as INV-4's end state, so issue #137 is
**not complete**.

### Issue #137 acceptance against S0-S2e

| Requirement | Status | Evidence (tests) |
|---|---|---|
| Scripted churn plan validates | Met at gateway level | `test_investigation_planning_gateway.py::test_one_scripted_call_returns_a_validated_non_executable_proposal`; `test_investigation_planning_repair.py::test_end_to_end_churn_planning_repairs_an_unknown_field_once` |
| Invalid initial plan repaired once | Met at gateway level | `test_investigation_planning_repair.py::test_invalid_initial_then_valid_repair_is_proposed_within_one_ledger` |
| Still invalid after one repair ends in `plan_invalid` | Met at gateway level | `test_investigation_planning_repair.py::test_two_invalid_candidates_are_the_terminal_plan_invalid`, `test_end_to_end_churn_unknown_field_twice_is_plan_invalid_with_task_ids` |
| 2 calls / 12k input / 2k output / 30 s / zero tools | Met | `test_two_maximal_stages_exactly_fill_the_planning_budget`, `test_the_repair_deadline_is_what_remains_of_the_original_thirty_seconds`, `test_illegal_sequences_are_refused_without_any_new_allowance`, tool/network spies in all three gateway suites, `test_planning_runtime_cannot_reach_sql_retrieval_models_or_test_code` |
| Planner input has no rows or passages (schema and titles only) | Met | `test_investigation_planning_prompt.py` allowlist, injection, and forged-field tests; `test_end_to_end_churn_planning_repairs_an_unknown_field_once` (no row values, oracle, or reference SQL in either prompt) |
| Estimate follows `CostEstimate.bound`; nothing guaranteed unless enforceable for every eligible provider, fallbacks included | Met for classification; no user-facing wording exists yet | `test_investigation_planning_prompt.py` reviewed-evidence cost tests; `test_investigation_planning_gateway_contracts.py` evidence tests; `test_unknown_pricing_does_not_block_planning_but_is_reported_honestly` |
| Not reachable from the router | Met (by design) | No router, API, or graph imports the gateway; `test_planning_runtime_cannot_reach_sql_retrieval_models_or_test_code` |
| Legacy workflows unaffected | Met | `test_langgraph_orchestrator.py`, `test_evaluation_baseline.py`, `python -m scripts.check_all` evaluations; no legacy module changed |
| `check_all` passes; no evaluation case removed or worsened | Met | `python -m scripts.check_all` 5/5; `evals/` untouched |

### Production exclusion (S2e)

- `test_investigation_boundaries.py::test_production_code_has_no_test_fakes_deadline_adapters_or_fabricated_records`
  scans `src/`, `apps/`, `packages/`, and `config/`: no `tests` import, no production
  `DeadlineBoundedModelAdapter` subclass, and no constructed `ReviewedPlanningCapability`,
  `ReviewedPlanningEvidence`, or `ProviderPricingTable` (only `planning_cost.py` derives effective
  `ProviderCostProfile` values).
- `test_planning_runtime_cannot_reach_sql_retrieval_models_or_test_code` forbids the planning
  modules from importing SQL, retrieval, document research, the execution gateway, graphs,
  checkpoints, real model clients, `sqlite3`, `socket`, `logging`, `importlib`, tests, or evaluators.
- Specialist rules ban the planning gateway, authority, context, admission, pricing, evidence, and
  `planner_port`; a Supervisor may import only `PlannerPort` and the result types.
- Each checker has synthetic negative cases, so an empty scan cannot pass vacuously.

### Remaining for S3/S4 and production

- **S3 (delivered):** `src/investigation/supervisor.py` drives
  `PlannerPort` (initial, then at most one repair) under the specialist boundary;
  `tests/test_investigation_supervisor.py`. Because a `PlanningCallResult` carries trusted ledger
  usage the Supervisor never sees, a Supervisor-detected failure is a separate fixed
  `SupervisorFailureCode` in `SupervisorOutcome`, not a fabricated result. S4 must pass
  `planner_port()`, never the gateway, to `plan_investigation`.
- **S4 (delivered):** proposal mode in `InvestigationGraphBuilder`;
  `src/orchestration/investigation_proposal.py:InvestigationProposalRun` builds the gateway from
  trusted inputs, passes only `planner_port()` to `plan_investigation`, owns `close(...)`, writes
  the plan and a write-once Supervisor-owned `proposal_record` bound to the planning ledger, and
  moves status through guarded lifecycle steps; `tests/test_investigation_proposal_graph.py`.
  `STATE_VERSION` stays 1 (optional channel, process-local checkpoints).
- **S5:** consolidated documentation (ADR 0024 amendment) and the readiness review.
- **Proposal withdrawal:** after `proposed`, the planning gateway is terminal and every later
  call or `close(...)` is `gateway_closed`. Cancelling or withdrawing a proposal that awaits
  approval is a lifecycle-owner transition from `awaiting_plan_approval` (S4 or later), not a
  gateway operation.
- **Production:** a reviewed real `DeadlineBoundedModelAdapter`, and a trusted owner and version
  format for reviewed evidence and pricing (open approvals 4-5). Until then, production planning is
  refused with `metering_unbounded`.
- **Deferred accounting display:** an over-input count records `budget_exhausted` without
  `exhausted_resource`; the 6,000 limit is a stage partition, not a ledger limit, so no ledger
  change was made.
