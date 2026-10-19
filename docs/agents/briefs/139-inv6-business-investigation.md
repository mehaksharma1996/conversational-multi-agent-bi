# S0 implementation brief: #139 INV-6 Business Investigation, hypotheses, tests, and verdicts

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/139
(part of umbrella #131; depends on #138 INV-5).
Tier: strongest. Check mode: `python -m scripts.check_all` (no API or web change, so not `--full`).

## S0 outcome

INV-6 extends the investigation from collecting evidence to testing business hypotheses:

- Hypotheses and their typed tests are proposed **before** approval, inside the plan, so the one
  human plan approval binds them (D1).
- Approved tests run through the INV-5 dispatcher and specialists, unchanged in their semantics.
- A deterministic Business Investigator turns verified evidence into exactly one verdict per
  hypothesis: `supported_association`, `untestable`, or `refuted`. Every number comes from
  gateway-issued evidence, never from a model, and nothing can express causation.

S0 changes this brief only. It implements no contract, gateway, specialist, graph, or test change.
Planning-budget constants and adapter contracts change only in S3, under the approved D2 and the
S3 gate.

## Baseline (verified 2026-10-19)

- Base: `origin/main` at `c9d72cd562c295b1b32049761ffeebdf399f9440`, the merge commit of INV-5
  PR #167.
- Branch `feat/iteration2-inv6-business-investigation`, worktree
  `work/agent-worktrees/139-inv6-business-investigation`. No other INV-6 branch or worktree
  existed before S0.
- S1 starts by recording `python -m scripts.check_all` on this base.

## Facts (verified against the code on 2026-10-19)

**Contracts (`packages/investigation/contracts.py`)**
- `TaskKind.HYPOTHESIS_TEST` and `HypothesisTestInput` already exist:
  - an ID matching `^h[0-9]{1,3}$`;
  - a method: `segment_comparison`, `stratified_comparison`, `trend_comparison`, or
    `document_check`;
  - fields, an optional query, and evidence references.
- `TASK_PROPOSERS[HYPOTHESIS_TEST]` is the Business Investigator and the Critic only, never the
  Supervisor. `TASK_EXECUTORS` lets the SQL Analyst or Document Research execute them.
- `EvidenceRef.kind` already allows `derived`.
- `InvestigationPlan` has `schema_version: Literal[1]`. `plan_digest` is SHA-256 of
  `model_dump(mode="json")` with sorted keys.

**Validation (`packages/investigation/validation.py`)**
- `validate_plan` checks every task with proposer=Supervisor and empty hypothesis and evidence
  sets, so a hypothesis task in a plan today fails `proposer_not_allowed` and `unknown_hypothesis`.
- For hypothesis tasks it also checks method-to-assignee (`document_check` goes to Document
  Research, every other method to the SQL Analyst), field names, and evidence references.
- `validate_follow_ups` allows the Business Investigator or Critic as proposer and caps follow-ups
  at 3 per round and `replan_rounds` rounds.

**State (`packages/investigation/state.py`)**
- Admitted follow-ups are stored in a Supervisor-owned channel. They are **not** covered by the plan
  digest, and nothing writes or executes them yet.
- `EvidenceRecord.kind` is `query | document`.
- Channels are fixed and an unknown channel is refused on restore. `STATE_VERSION` is 1.
- Writers are `intake`, `lifecycle`, `supervisor`, `approval`, `dispatcher`, and `terminal`.

**Planning**
- `validate_planning_draft` (`packages/investigation/planning.py`) refuses any plan version other
  than `SCHEMA_VERSION`.
- The prompt (`src/orchestration/planning_prompt.py`, `inv4-planning-prompt-v1`) is rendered from
  `PlanningMetadata` only, and lists only task kinds the Supervisor may propose. It is **not** in
  `evals/v1/prompt-registry.json`.
- Planning limits:
  - `PLANNING_LIMITS`: 2 model calls, 12,000 input and 2,000 output tokens, 30 seconds;
  - stage partitions (`packages/investigation/gateway.py`): `PLANNING_STAGE_INPUT_TOKENS` 6,000 and
    `PLANNING_STAGE_OUTPUT_TOKENS` 1,000;
  - adapter admission (`src/llm/bounded.py`, and `ReviewedPlanningCapability` in `gateway.py`)
    requires `bound.max_output_tokens == PLANNING_STAGE_OUTPUT_TOKENS` exactly.

**Gateway (`src/orchestration/tool_gateway.py`)**
- `_sql_question` builds a generic text for hypothesis tests ("Test hypothesis h1 with a … over
  these fields: …"), with no filters, periods, metric, or direction.
- `_document_instruction` supports `document_check`.
- A task with evidence references and no trusted policy criteria fails `policy_refused` (INV-5
  decision R4 deferral).
- Typed payloads (INV-5 S2) are verified at issuance: query cells become JSON scalars, SQL NULL and
  NaN become `None`, and `truncated` means the result reached the 500-row limit.

**Execution (INV-5)**
- `InvestigationExecutionRun` (`src/orchestration/investigation_execution.py`) refuses any plan
  task outside `_SPECIALISTS` (keyed by `TaskKind`) with `plan_not_executable`.
- `packages/investigation/dispatch.py:ordered_tasks` already covers plan tasks and then admitted
  follow-ups. Dependency and retry policies are kind-agnostic.

**Derived evidence**
- `DerivedEvidencePayload` (`packages/investigation/evidence.py`) is contract-only: operations
  `difference | ratio | share | rate_difference`, source evidence IDs, and a value.
- `verify_payload` refuses every derived payload.

**Churn fixture**
- The real CSV columns include `quarter_start`, `region`, `customer_segment`, `plan_tier`,
  `tenure_months_start`, `active_at_start`, `churned`, `avg_resolution_hours`, and
  `support_tickets`. No column records a retention intervention.
- The INV-4/INV-5 planning fakes use outdated names (`segment`, `tenure_months`); INV-6 must plan
  against the real schema.
- Data, computed by S0 from the CSV over at-risk rows:

  | Hypothesis | Measure | 2026-Q1 | 2026-Q2 |
  |---|---|---|---|
  | H1 | SMB Basic churn | 30/500 = 0.06 | 70/500 = 0.14 |
  | H2 | APAC churn | 20/400 = 0.05 | 36/400 = 0.09 |
  | H2 | APAC mean resolution hours | 34.02 | 48.75 (other regions about 19 to 21) |
  | H4 | Enterprise churn | 14/700 = 0.02 | 14/700 = 0.02 |

- `oracle.json` hypotheses:
  - H1, SMB Basic churn rose with the pricing change: `supported_association`;
  - H2, APAC churn with the support backlog: `supported_association`;
  - H3, the playbook reduced churn: `untestable`;
  - H4, Enterprise churn increased in 2026-Q2: `refuted`.

  Its tolerances are counts exact, rates 0.0001, currency exact to the cent. `oracle.json` and
  `reference.sql` are evaluator-only.

## Approved decisions (2026-10-19)

| ID | Decision |
|---|---|
| D1 | **Plan-embedded hypotheses.** Hypotheses and their typed tests are in the plan before approval, and the approval digest binds every hypothesis definition and executable test specification. Hypothesis tasks are validated with the Business Investigator's own permissions; the Supervisor gains no general authority to create specialist tasks. No post-approval task mutation, and no unapproved follow-up is executed. |
| D3 | **Explicit v2 plan.** New hypothesis-bearing plans are `schema_version: 2`. v1 canonicalization and every existing v1 approval digest stay byte-identical. Version dispatch is strict, and ambiguous or malformed versions are refused. Checkpoint compatibility is evaluated separately; `STATE_VERSION` is not bumped automatically. |
| D4 | **Deterministic derived evidence.** Derivation is owner-only, from verified payloads, with full source provenance and scope and task binding, over a fixed set of operations, with strict validation. No model-authored number is ever an observation. Derived payloads stay out of checkpoints and telemetry beyond their content-free metadata. |
| D2 | **Revised planning output budget (approved 2026-10-19).** 2 model calls; input 6,000 per call and 12,000 cumulative; **output 2,000 per call and 4,000 cumulative**; the original 30-second deadline; 0 tool calls; an initial attempt plus at most one repair. The budget contract, stage reservations, cost estimates, exact adapter admission, tests, and docs change together, and capability evidence for the old 1,000 bound must fail exact admission. No unreviewed production adapter is enabled, no capability evidence is fabricated, and the deadline is not extended. The S3 gate below applies. |

## D2 measurement (planning budget)

**Method (offline and read-only; no provider call).** Scratchpad script, not committed:

- the production renderer (`render_initial_prompt`, `render_repair_prompt`) on real churn
  `PlanningMetadata`: 24 real CSV fields with logical types, four document titles, and the
  full-investigation question;
- a draft v2 instruction addendum (hypothesis and typed-test vocabulary, 1,195 bytes);
- compact (no-whitespace) v2 model responses for H1 to H4.

There is no exact Gemini tokenizer offline, so three measures are reported:

- **bytes:** for ASCII text, a hard upper bound for BPE or SentencePiece tokenizers with byte
  fallback, before the adapter's system text;
- **chars/4:** Google's typical figure for Gemini;
- **WordPiece:** the local MiniLM tokenizer, which over-counts JSON punctuation (a pessimistic
  proxy).

The admitted adapter's exact pre-call count stays the runtime authority.

| Measured text | Bytes | chars/4 | WordPiece | Stage cap |
|---|---:|---:|---:|---:|
| v1 initial prompt (production) | 4,233 | 1,059 | 1,420 | 6,000 in |
| v1 repair prompt, 32 feedback items | 5,967 | 1,492 | 2,246 | 6,000 in |
| **v2 initial prompt** (v1 + addendum) | 5,428 | 1,357 | 1,831 | 6,000 in |
| **v2 repair prompt** (32 items + addendum) | 7,162 | 1,791 | 2,657 | 6,000 in |
| v1 baseline response (5 evidence tasks) | 1,849 | 463 | 705 | 1,000 out |
| **v2 full response** (5 evidence tasks, 4 tests, H1 to H4) | 5,047 | 1,262 | 2,112 | 1,000 out |
| v2 compact response (2 document tasks, 4 tests, H1 to H4) | 4,082 | 1,021 | 1,724 | 1,000 out |
| of which: the hypotheses section | 746 | 187 | 293 | |
| of which: the four typed test tasks | 2,439 | 610 | 1,107 | |

**Results**

- **Input fits with wide margin.** Even the pessimistic proxy for the worst repair prompt is
  2,657 tokens against the 6,000-token stage partition. A two-call envelope is at most
  2 × 2,657 = 5,314 of 12,000.
- **Output does not fit.** The 1,000-token stage cap is below every estimate of a single v2
  response: 1,262 (chars/4) to 2,112 (WordPiece) for the full plan, and still 1,021 to 1,724 for
  the compact one.
  - JSON usually tokenizes nearer 3 characters per token than 4, so roughly 1,700 tokens is the
    realistic full response.
  - The two-call output total does not help: the stage cap applies to each call, and the repair
    must be a complete new response.
- **The current caps cannot support the format.** A response would be cut off, fail to parse, and
  end `plan_invalid` or `provider_failure`.

**Recommendation (smallest justified change; not applied).**

- **Output:**
  - `PLANNING_STAGE_OUTPUT_TOKENS`: 1,000 to **2,000**. This covers the realistic full response
    (about 1,700) with about 15% headroom. The byte upper bound, 5,047, is not a token count.
  - `PLANNING_LIMITS.output_tokens`: 2,000 to **4,000** (two stages).
  - It is still well below the validator-maximal response (12 tasks and 6 hypotheses at full text
    lengths, roughly 5,000 tokens), which correctly stays unfittable.
- **Input:** keep `input_tokens` 12,000, the stage partition 6,000, and 2 model calls.
- **Wall clock:** keep 30 seconds provisionally. Producing 2,000 output tokens per call may need
  10 to 20 seconds on a fast model. The wall-clock limit must be confirmed by a reviewed live
  latency measurement before any production activation (#147). Raising it to 45 seconds is the
  fallback if that measurement fails.
- **Also reduce the format:**
  - shared plan-level defaults for the metric (`churned` over `active_at_start`) and the period
    (`quarter_start`, baseline, comparison), cutting about 40% from the test tasks;
  - short `success_criterion` texts.

  Even then, the compact estimate (about 1,000 to 1,700 tokens) stays above 1,000, so the cap
  increase is still needed.

**Implications of the recommendation.**

- **Cost classification.** `estimate_planning_cost` prices the fixed `PLANNING_LIMITS` at the
  highest eligible price. The output part of the figure doubles; the classification logic and
  labels (`enforced_upper_bound`, `indicative`, `unavailable`) are unchanged.
- **Reviewed adapter admission.** Admission and `ReviewedPlanningCapability` require
  `max_output_tokens` to equal the stage cap exactly, so every reviewed planning capability or
  evidence record must be re-issued for the 2,000 cap with a new evidence version. Old evidence is
  refused, not silently stretched.
- **Provider-enforced deadline.** The semantics are unchanged (confirmed termination;
  `metering_violation` otherwise). A larger cap lengthens worst-case generation inside the same
  deadline, hence the latency check.
- **Regression.** Tests that pin `PLANNING_LIMITS`, the stage partitions, 1,000-token envelope
  bounds, or cost figures need reviewed updates in the INV-4 planning, gateway, proposal-graph,
  budget, and bypass suites. Execution limits, the INV-5 suites, and `evals/` are untouched.
- **Alternative.** Keep 1,000 and add a third planning stage dedicated to hypotheses. Rejected: it
  changes the reviewed two-call sequence (initial plus at most one repair) and the
  `planning_sequence_violation` rule, which is a larger change than a cap.
- **Decision (2026-10-19).** The recommendation is approved: 2,000 per call and 4,000 total,
  everything else unchanged.

**Tokenizer availability and the S3 gate.**

- No reviewed provider tokenizer is available in this environment:
  - `google-genai`'s local tokenizer needs `sentencepiece` (not installed; installing it, and
    downloading its model, would be a dependency change);
  - the remote `count_tokens` needs provider credentials that agents may not read.
- The measurements above are therefore proxies, not provider counts. The admitted adapter's exact
  pre-call count stays the runtime authority, and a response over the cap fails closed
  (`plan_invalid` or `provider_failure`, never a truncated plan).
- The full v2 response measured 2,112 tokens under the pessimistic WordPiece proxy, which is above
  2,000. S1 therefore adopts the compact format (shared plan-level metric and period defaults,
  short criteria).
- **S3 gate:** before S3 changes any budget, the compact full churn response is re-measured with
  all three proxies. S3 proceeds only if every proxy is at most 1,800 tokens (10% headroom under
  2,000). Otherwise work stops and is reported, without relaxing the limit.

## Contract and authorization boundaries

**Hypothesis contracts (S1, `packages/investigation/hypotheses.py`).** Frozen, strict, extra fields
forbidden, JSON round-trip.

- **`Hypothesis`:**
  - `id` (`h` and 1 to 3 digits), unique in the plan;
  - `statement`, untrusted display text of at most 300 characters, never parsed or executed;
  - `relationship: Literal["association"]`, the only value, so causation is unrepresentable;
  - `testability: "testable" | "untestable"`;
  - `missing_data`, at most 4 short texts, required only when untestable;
  - `tests`, the IDs of its `hypothesis_test` tasks: non-empty when testable, empty when
    untestable;
  - `context_tasks`, IDs of approved `document_research` tasks whose citations the verdict may use
    as context.
- **`HypothesisTestSpec`** (`HypothesisTestInput` v2, used only in v2 plans):
  - `metric`: a `rate` (an `event` boolean field over a `population` boolean field) or a `mean` (a
    numeric `value` field);
  - `filters`: at most 4 equality filters on approved fields, with literal values only;
  - `period`: a field plus `baseline` and `comparison` values;
  - optional `comparison_group`, an equality filter for "higher than another group" tests;
  - `claim`: a `direction` of `increase`, `decrease`, or `higher_than`, a strict-float
    `min_effect` ≥ 0, and a `min_denominator` ≥ 1.

  Every field the spec names must be in the task's `fields` and in the confirmed schema.
  `document_check` tests keep `query` and carry no metric, filters, or period.
- **`Observation`** (computed only by trusted code):
  - the source evidence ID;
  - the matched key values;
  - exact integer counts (numerator, denominator) or the sum and count;
  - the value as an exact rational rendered to a fixed precision.
- **`Verdict`:**
  - `hypothesis_id`;
  - `verdict: "supported_association" | "untestable" | "refuted"`;
  - fixed `reasons` codes;
  - cited `evidence_ids` (query, document, derived);
  - `test_task_ids`.

  No free text, and no `inconclusive` value: insufficient or unusable evidence is `untestable` with
  a reason (matching the oracle's vocabulary).
- **`VerdictRecord`** (state-safe): `hypothesis_id`, `verdict`, `reasons`, `evidence_ids`, and
  `test_task_ids`. IDs and codes only; no numbers or text.

**Plan v2 (S1, D3).**

- `InvestigationPlanV2` has `schema_version: Literal[2]`, all v1 fields, and
  `hypotheses: tuple[Hypothesis, ...]` (at most 6).
- `PlanTask` inputs in v2 may be the v2 `HypothesisTestInput`.
- A `parse_plan(raw)` dispatcher:
  - requires an exact integer `schema_version` of 1 or 2 (refusing a missing version, `True`,
    `1.0`, strings, and unknown versions);
  - validates with exactly that version's model;
  - never tries one version after the other.
- `plan_digest` keeps the v1 encoding byte-for-byte. A v2 digest is the same canonical encoding of
  the v2 dump, and the differing `schema_version` keeps the two digest domains apart. A pinned test
  asserts the existing churn v1 digest is unchanged.
- A v1 plan can never carry hypotheses or v2 test specs.

**Validation (S1/S3, D1).**

- `validate_plan` gains a v2 path. Evidence tasks are still checked with proposer=Supervisor.
  `hypothesis_test` tasks are checked with proposer=Business Investigator, and only if referenced
  by a plan hypothesis's `tests`.
- New checks:
  - every hypothesis test belongs to exactly one hypothesis, and every referenced task exists;
  - untestable hypotheses have no tests and do have `missing_data`;
  - `context_tasks` are `document_research` tasks in the plan;
  - spec fields are in the schema;
  - `evidence_refs` stay empty in v2 tests, so R4's document-derived policy criteria remain
    deferred and SQL runs as `not_applicable`;
  - the task count stays under `limits.task_attempts`.
- `TASK_PROPOSERS` is unchanged; the Supervisor gains no hypothesis authority.

**Who may do what.**

| Component | May | Never |
|---|---|---|
| Planning call (proposal phase) | Emit plan v2 text, validated deterministically; inputs are the question, schema, and titles only | See rows, passages, results, or the oracle |
| Business Investigator (`src/investigation/business_investigator.py`) | Read the approved plan and a trusted, verified evidence bundle the owner hands it; return `Verdict` proposals | Call any gateway or port operation, a model, SQL, retrieval, the ledger, or authority; create, mutate, or admit tasks; write state |
| SQL Analyst / Document Research | Run approved hypothesis tests exactly as INV-5 tasks | Decide retries, skips, or verdicts |
| Run owner (`investigation_execution.py`) | Route hypothesis tests by method and assignee; call `derive_evidence`; validate and write verdicts through a guarded step | Construct approvals or authority; execute unapproved tasks |
| Gateway | Issue derived evidence after recomputation | Accept a caller-supplied number |

Admitted follow-ups stay unused: INV-6 writes none and executes none (the Critic and its approval
design are INV-7 and #143).

## Evidence-to-verdict validation (S2, `packages/investigation/verdicts.py`)

`evaluate_hypothesis(hypothesis, tests, bundle)` is pure and deterministic. The bundle holds the
final validated state view and the gateway's verified typed payloads.

1. **Testability.** An untestable hypothesis gives `untestable/required_data_absent`, citing only
   valid context citations.
2. **Task outcomes.** Each test task must have `succeeded` with exactly one query (or document)
   evidence. A failed, skipped, refused, or cancelled test gives `untestable` with
   `test_not_completed` (plus its category).
3. **Evidence identity.**
   - The evidence ID must be in `view.evidence`.
   - `verify_payload` must pass.
   - The record's `task_id` must be the test task, and the scope dataset or collection must match.

   Otherwise the result is `untestable/evidence_inconsistent`.
4. **Truncation.** `payload.truncated` gives `untestable/evidence_truncated`; grouped results
   should never approach 500 rows.
5. **Shape and keys.**
   - The result must expose the spec's period field and every filter field as columns, plus the
     raw counts (`numerator` and `denominator`, or `value_sum` and `value_count`).
   - Exactly one row must match each (period, group) key.
   - A missing key column or row, a duplicate key row, or extra rows that match a key give
     `untestable/evidence_inconsistent`. This catches SQL that answered a different question.
6. **NULL and type rules.**
   - Counts must be non-negative integers, and the numerator must not exceed the denominator.
   - `None` (SQL NULL or NaN) in a key or count gives `evidence_inconsistent`; it is never 0 or an
     empty string.
   - A denominator below `min_denominator` gives `insufficient_denominator`.
7. **Computation.** Exact `Fraction` arithmetic: rate = numerator / denominator, mean = sum / count,
   effect = comparison − baseline (or group − comparison group). A model-supplied rate column is
   never trusted; if present it must agree within 0.0001 or the result is `evidence_inconsistent`.
8. **Decision.** In the claimed direction:
   - effect ≥ `min_effect` gives `supported_association`;
   - effect ≤ 0 (no change, or the opposite direction) gives `refuted`;
   - 0 < effect < `min_effect` gives `untestable/effect_below_threshold`.

   Several tests for one hypothesis: all must support for `supported_association`; any refutation
   gives `refuted`; otherwise `untestable`.
9. **Contradictory evidence.** Two evidences for the same metric and key that disagree give
   `untestable/conflicting_evidence`.
   - Document text never overrides data: a document claim (the board memo) is untrusted context
     and can neither support nor refute a numeric claim.
   - A hypothesis whose `context_tasks` lack valid citations
     (`document_citations_valid`) is reported with the reason `context_unverified`. It cannot
     receive `supported_association` when the approved spec marks that context as required.
10. **Derived evidence (D4).** For every observation and effect used, the owner calls the owner-only
    gateway operation `derive_evidence`.
    - Operations: `rate`, `mean`, `difference`, `rate_difference` (S1 may narrow
      `DerivedOperation`).
    - Inputs: source evidence IDs and the spec's key selectors.
    - The gateway recomputes from the payloads it holds, issues an `ev_` ID, and records
      `EvidenceRecord(kind="derived")` with `DerivedProvenance`: source IDs (which must exist in
      the same scope, with succeeded tasks), the operation, and the task ID and attempt of the test
      task.
    - The derived payload (with its value) stays in process; state holds no value.
    - `verify_payload` gains a derived branch that requires equal sources and operation and a
      recomputation within 0.0001.
11. **Final check.** `validate_verdict(verdict, bundle)` recomputes everything from scratch and
    refuses unless:
    - every cited ID exists in the view;
    - every derived value recomputes;
    - the verdict equals the evaluator's for the same inputs.

    The run owner writes only validated verdicts.

Content separation: checkpoints hold verdict records and derived-evidence metadata (IDs, operation,
counts of sources) only. Rows, rates, means, passages, and model text stay in process. No telemetry
or audit field is added.

## State and compatibility (S1/S4; D3 checkpoint evaluation)

- **Additive vocabulary** (each refused by an older reader rather than misread):
  - `EvidenceRecord.kind` gains `derived` with `DerivedProvenance`;
  - `GatewayTool` gains `derive_evidence`;
  - a new write-once-per-hypothesis channel `verdicts: dict[hypothesis_id, VerdictRecord]`;
  - a new `Writer` `investigator`, owning only `verdicts`.
- **Invariants:**
  - verdict IDs are plan hypotheses;
  - every cited evidence ID exists;
  - verdicts are written only while `running` and after every test task is terminal;
  - derived sources exist, are in scope, and belong to succeeded tasks.
- **`STATE_VERSION` stays 1,** following the INV-4 (`proposal_record`) and INV-5 (`skip_task`)
  pattern:
  - every pre-INV-6 checkpoint restores unchanged (a restore-compatibility test pins this);
  - plans in state are parsed through `parse_plan`, so a v1 thread restores as v1;
  - a v2 or verdict-bearing state is refused by older readers (`unknown_channel` or
    `invalid_value`).

  A bump is needed only if a v1 state's meaning changes, which none of this does. Durable
  checkpoints (#149) must carry this vocabulary in their migration design.

## Execution integration (S4)

- **Routing.** `_SPECIALISTS` becomes a route table keyed by (kind, method):
  - `hypothesis_test` with a SQL method goes to `SQLAnalyst`;
  - `document_check` goes to `DocumentResearcher`;
  - the assignee must match the method, otherwise `plan_not_executable`.
- **Handoff.** Accepts v2 plans through `parse_plan`. Execution, retries, cancellation, budget,
  skips, and terminal semantics are unchanged; hypothesis tests are ordinary tasks to the
  dispatcher.
- **SQL question.** `_sql_question` renders the typed spec deterministically:
  - the metric and the counts to return;
  - filters and period values as quoted literals;
  - the required output columns.

  The SQL prompt builder (`build_sql_prompt`, which is in the prompt registry) is not edited, so no
  prompt fingerprint changes. The guard, handles, fingerprints, and approval rules are unchanged.
- **Verdict step.**
  - On a normal or partial end (never after a defect, cancellation, or budget exhaustion), the
    owner:
    1. derives evidence;
    2. runs the Business Investigator;
    3. validates each verdict;
    4. writes `verdicts` in one guarded `investigate` step;
    5. then closes as `completed`.
  - **Wording** for the stopped cases: after cancellation, budget exhaustion, or a closing refusal,
    no verdict is written; the report states `verdicts_not_evaluated`.
  - **Order:** the `investigate` step runs before `close(completed)`, so `derive_evidence` runs
    while the gateway is open and charges nothing.
  - **Final status:** unchanged, decided by tasks (verdicts do not change it).
- **Interim report.** Gains `ReportVerdictRow` (hypothesis ID, verdict, reasons, evidence IDs) and
  fixed limitation codes (`verdicts_not_evaluated`, `effect_below_threshold`, …). Statements are
  never rendered as findings. Labels stay "interim, unverified, association only".

## Stages and exit criteria

| Stage | Scope | Exit criteria |
|---|---|---|
| S0 | This brief; D2 measurement | Reviewed; documentation checks pass; approved separately |
| S1 | `hypotheses.py` contracts; plan v2, `parse_plan`, and v1 digest pinning; v2 `HypothesisTestInput`; state vocabulary (derived kind and provenance, `derive_evidence`, `verdicts`, `investigator`) with invariants | Baseline recorded; contract, round-trip, version-dispatch, v1-digest-unchanged, and restore-compatibility tests; `check_all` green |
| S2 | `verdicts.py`: the pure evaluator and `validate_verdict`; derived-payload verification | Adversarial tests (wrong ID, altered number, NULL, zero or small denominator, truncated, duplicate or extra key rows, conflicting evidence, document-only claims, causal wording); determinism |
| S3 | Proposal side: v2 planning draft, prompt `inv6-planning-prompt-v2` (real-schema fixtures), v2 `validate_plan` path; the approved D2 budget, after the S3 gate passes | Planning, prompt, repair, and proposal-graph tests; prompt pinned; evaluations unchanged |
| S4 | Gateway `derive_evidence` and the typed-spec SQL question; route table; the `investigate` step; `business_investigator.py`; report verdict rows | Gateway, bypass, boundary, state-restore, and lifecycle tests; the Business Investigator passes the specialist boundary checker; all INV-5 suites unchanged and green |
| S5 | Scripted churn acceptance (H1 to H4) on the real CSV and four-PDF index; adversarial end-to-end tests | The acceptance matrix below |
| S6 | ADR 0024 Amendment 4, `docs/architecture/investigation-workflow.md`, this brief's readiness section | `check_all` green; evaluations unchanged; readiness review |

S1 and S2 do not depend on D2; S3 does.

## File-level plan

**New**
- `packages/investigation/hypotheses.py`, `packages/investigation/verdicts.py`
- `src/investigation/business_investigator.py`
- tests:
  - `tests/test_investigation_hypotheses.py` (contracts, plan v2, version dispatch, digests);
  - `tests/test_investigation_verdicts.py` (evaluator, validator, adversarial);
  - `tests/test_investigation_business_investigator.py`;
  - `tests/test_investigation_hypothesis_planning.py`;
  - `tests/test_investigation_derived_evidence.py`;
  - `tests/test_investigation_hypothesis_execution.py`;
  - `tests/test_investigation_churn_hypotheses.py`;
  - `tests/investigation_churn_schema.py` (real-schema `PlanningMetadata` and plan builders).

**Modified**
- `packages/investigation/contracts.py`: v2 plan and test input, `parse_plan`.
- `packages/investigation/validation.py`: v2 path.
- `packages/investigation/planning.py`: draft v2.
- `packages/investigation/state.py`: derived kind and provenance, `derive_evidence`, `verdicts`,
  `investigator`, invariants.
- `packages/investigation/evidence.py`: derived verification.
- `packages/investigation/interim_report.py`: verdict rows and codes.
- `packages/investigation/__init__.py`.
- `src/orchestration/planning_prompt.py`: v2 prompt.
- `src/orchestration/tool_gateway.py`: `derive_evidence`, `_sql_question`.
- `src/orchestration/investigation_execution.py`: route table, `investigate` step, v2 handoff.
- `src/investigation/__init__.py`.
- `tests/test_investigation_boundaries.py`: the Business Investigator's import rules; ban
  `derive_evidence` in specialist modules.
- `tests/test_investigation_bypass.py`.
- S6 only: `docs/adr/0024-multi-agent-investigation.md`, `docs/architecture/investigation-workflow.md`.
- S3 (D2): `packages/investigation/budget.py`, `packages/investigation/gateway.py`,
  `src/llm/bounded.py`, and their pinned tests.

## Do not touch

- `apps/**`, OpenAPI, and the web client;
- `evals/**` and every baseline, `oracle.json`, `reference.sql`, `sample_data/**` (the one
  exception is a reviewed registry entry for the v2 planning prompt in S3, see the outstanding
  decisions);
- `src/agents/sql_agent.py` and `src/agents/rag_agent.py` (registered prompt builders);
- the question graph and legacy routes;
- the execution limits;
- telemetry and audit allowlists;
- dependency and lock files, containers;
- routing (#142), approvals (#143), and the API (#144).

## Scripted churn acceptance matrix (S5)

The scripted planning response is a v2 plan on the real schema with:

- evidence tasks `trend`, `segments`, `pricing`, `support`, `playbook`;
- tests `h1_rate`, `h2_rate`, `h2_resolution`, `h4_rate`;
- hypotheses H1 to H4.

Scripted SQL returns counts per (period, group); the scripted document answer cites real sources.

| # | Criterion | Assertion |
|---|---|---|
| A1 | H1 is `supported_association` | SMB Basic 30/500 then 70/500; effect 0.08 ≥ the approved `min_effect`; the pricing notice page 1 citation is valid context |
| A2 | H2 is `supported_association` | APAC 20/400 then 36/400, and APAC mean resolution 34.02 then 48.75 h; both tests support; the support-document citation is valid |
| A3 | H3 is `untestable` | `required_data_absent`; no SQL task exists for H3; the playbook citation is context only |
| A4 | H4 is `refuted` | Enterprise 14/700 then 14/700; effect 0 ≤ 0; the board memo's "doubled" claim is untrusted and changes nothing |
| A5 | Cited IDs exist and numbers match | Every verdict evidence ID is in the final state; each derived value recomputes; values equal `oracle.json` facts (counts exact, rates within 0.0001), read by the test only |
| A6 | Determinism | Identical inputs give byte-identical verdicts and report JSON; verdicts are independent of payload order |
| A7 | Tampering fails | A wrong evidence ID, an altered derived value, a swapped task binding, or a forged `Verdict` all fail `validate_verdict` |
| A8 | Unusable evidence is never confident | Truncated, NULL-count, missing-row, duplicate-row, conflicting, failed, or skipped tests give `untestable` with the matching reason, never `supported_association` |
| A9 | Approval binding | Changing any hypothesis or test spec changes the v2 digest, so the approval does not match and execution is refused; v1 digests are unchanged; no follow-up runs |
| A10 | Content and isolation | No rows, rates, SQL, passages, or model text in checkpoints or decisions; `restore_state` passes at every checkpoint; the oracle-isolation scan covers the new modules; no production module reads `evals/` |
| A11 | Causation cannot be expressed | No verdict value, reason, or report field states causation; a causal statement in a hypothesis is display-only, and its verdict concerns the typed association |
| A12 | Fail-closed and regressions | A production client is refused with `metering_unbounded`; every INV-3 to INV-5 suite and evaluations are unchanged |

Expected verdicts appear only in the acceptance tests, never in production code: the verdicts
follow from the approved specs, the thresholds, and the evidence.

## Security and regression strategy

- **Boundaries.** The Business Investigator module is checked like a specialist, plus a ban on
  every port operation, `derive_evidence`, the gateway, and models.
- **Purity.** The pure modules import only `packages.investigation`, pydantic, and the standard
  library.
- **No new paths.** No new model path, route, flag, budget envelope, or retry.
- **Approval.** Hypothesis specs reach execution only through an approved plan digest. A structural
  test keeps "no production module constructs approval or authority".
- **Untrusted text.** Document and model text never reaches task authoring or dispatch: planning
  sees titles only, and verdicts read typed payload cells and citation locations only.
- **Regressions.** Every existing investigation, gateway, planning, proposal, and legacy LangGraph
  suite and evaluations stay green. Behaviour pinned by INV-5 tests stays unchanged.
- **Carried from the INV-5 review:**
  - NULL preservation: NULL is never 0;
  - truncation: "reached the limit" means possibly incomplete;
  - terminal cleanup: verdicts are written only before the normal close, and the close-out
    categories are unchanged.

## Outstanding decisions and risks

1. **D2** is approved (2,000/4,000). The open part is the S3 gate: no reviewed provider tokenizer
   is available, so S3 proceeds on the proxy re-measurement rule above. A live, reviewed token
   measurement is still owed before any production activation.
2. **Planning prompt registry.** S3 versions the prompt as `inv6-planning-prompt-v2`, with its text
   and digest pinned by tests. Registering it in `evals/v1/prompt-registry.json` changes the
   evaluation baseline's prompt fingerprints. It is done only if the evaluation gate stays green
   with an explicit, reviewed registry entry; otherwise it is reported as a follow-up. Existing
   baselines are never degraded.
3. **`min_effect` and `min_denominator`** are model-proposed in the plan and human-approved. A
   too-low threshold makes support easy, so the approval UI (#144) must show them.
   `MIN_DENOMINATOR_FLOOR` is a configurable constant, initially 30, enforced by validation. A
   floor is a data-sufficiency rule, not a statistical-significance test.
4. **Filter literals** written by the model reach the SQL question. Defences: human approval, the
   read-only guard, and the verdict's key matching. Literals are restricted to short strings with
   no quotes or control characters.
5. **The generic SQL model may return the wrong shape,** which makes the verdict `untestable`.
   Scripted tests cover the shape contract; live quality is an evaluation question (#145, #147).
6. **H2 is two-part** (churn and resolution). "All tests must support" is conservative; an
   approval reviewer may want per-test weighting later (INV-7/INV-8).
7. **Overlap with the verified report (#141):** INV-6 adds verdict rows only, with no prose
   synthesis.
8. **Production remains fail-closed** without a reviewed bounded adapter. The D2 change, if
   approved, needs re-issued reviewed evidence.

## Estimated complexity and critical path

- **Size:** large; roughly 6,000 to 8,000 lines including tests, larger than INV-5.
- **Critical path:** D2, then S1, then S3, then S4, then S5.
- **Parallel:** S2 can proceed in parallel with S3 once S1 lands.

## S0 acceptance

- [x] `origin/main` verified at `c9d72cd`, containing the INV-5 PR #167 merge; isolated INV-6
      branch and worktree created from it.
- [x] Contracts, validation, planning, state, gateway, execution, evidence, report, churn fixture,
      oracle, and evaluation coupling reviewed against the code.
- [x] D1, D3, and D4 recorded; D2 measured, with a precise recommendation.
- [x] Stages, files, boundaries, validation design, compatibility, acceptance matrix, and risks
      documented.
- [x] No contract, gateway, specialist, graph, test, evaluation, ADR, or GitHub change in S0; no
      commit.

## Readiness (S6, 2026-10-19)

Delivered locally on `feat/iteration2-inv6-business-investigation`, one commit per stage after
`c9d72cd`:

| Stage | Delivered |
|---|---|
| S0 | This brief, with D1 to D4 and the D2 measurement |
| S1 | `hypotheses.py`, plan v2 and `parse_plan`, the v1 digest pin; state vocabulary (derived kind, `derive_evidence`, `verdicts`, `investigator`) and invariants |
| S2 | `verdicts.py` (`evaluate_hypothesis`, `validate_verdict`); the derived payload contract |
| S3 | v2 drafts, Business Investigator proposer validation, `inv6-planning-prompt-v2`, the 2,000/4,000 output budget |
| S4 | Gateway `derive_evidence` and the typed-spec question; method routing; `business_investigator.py`; the `investigate` step; report verdict rows |
| S5 | H1 to H4 acceptance on the real data, and adversarial end-to-end tests |
| S6 | ADR 0024 Amendment 4, the workflow document, this section |

Results:

- H1 and H2 `supported_association`, H3 `untestable` (`required_data_absent`), H4 `refuted`.
- Counts and rates equal `oracle.json` facts within its tolerances, read only by the tests.
- The S3 gate passed: the compact churn response is 825 to 1,369 tokens by every proxy, under
  1,800.

Deviations and choices:

- The v2 prompt is versioned and pinned by golden digests, but not registered in
  `evals/v1/prompt-registry.json` (a reviewed baseline change, left as a follow-up).
- The mean metric ignores NULL values (`value_count` counts non-NULL rows).
- A decisive verdict whose observation has no matching derived evidence is recorded as
  `untestable/evidence_missing`.

Known limits:

- tokens are measured by proxy only;
- thresholds are model-proposed and human-approved;
- all tests of a multi-test hypothesis must support;
- process-local checkpoints and replay protection, as in INV-5.
