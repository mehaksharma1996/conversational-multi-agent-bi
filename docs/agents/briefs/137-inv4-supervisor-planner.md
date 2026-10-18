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
