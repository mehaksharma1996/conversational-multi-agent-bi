# S0 implementation brief: #140 INV-7 Critic Agent and bounded feedback/replanning

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/140
(part of umbrella #131; depends on #139 INV-6).
Tier: strongest. Check mode: `python -m scripts.check_all` (no API or web change, so not `--full`).

## Outcome

After INV-7, an approved v2 investigation does not stop at its first verdicts:

- a Critic reviews them against verified evidence;
- the Supervisor may admit at most two bounded, re-validated, separately authorized follow-up
  rounds;
- the follow-ups run through the same dispatcher and gateway;
- verdicts are re-evaluated on the new evidence, and the run terminates deterministically.

The scripted churn run detects the missing tenure stratification of H1, runs exactly one
tenure-stratified SQL follow-up, and keeps H1 at `supported_association` on the standardized
evidence.

## Baseline (verified 2026-10-20)

- Base: `origin/main` at `fe00f9098508576e140b35eb24f37d8ec4e2db63`, the merge of INV-6 PR #174.
- Branch `feat/iteration2-inv7-critic-replanning`, worktree
  `work/agent-worktrees/140-inv7-critic-replanning`. No other INV-7 branch or worktree existed.

## Facts (verified against the code on 2026-10-20)

**Already present, unused:**
- `TASK_PROPOSERS` lets the Critic propose every task kind
  (`packages/investigation/contracts.py`).
- `validate_follow_ups` (`packages/investigation/validation.py`) applies the following checks:
  - the proposer is the Critic or the Business Investigator;
  - at most 3 requests per round, and rounds below `limits.replan_rounds`;
  - duplicate IDs and cycles are checked against the plan and earlier follow-ups;
  - total tasks stay within `task_attempts`.
- The state channel `admitted_follow_ups` (`packages/investigation/state.py`):
  - is Supervisor-owned and append-only, and validated as v1 `PlanTask`;
  - is not covered by the plan digest;
  - nothing writes it.
- Follow-ups are already seen by the rest of the pipeline:
  - `InvestigationStateView.admitted_task` and `dispatch.ordered_tasks` include them;
  - the gateway's `_admitted` therefore admits a follow-up exactly like a plan task.
- `BudgetLimits`/`BudgetUsage` have `replan_rounds` and `critic_rounds` (execution defaults 2 and 2),
  but nothing reserves them.

**Execution owner (`src/orchestration/investigation_execution.py`).**
- On `FINISH_RUN` for a v2 plan it:
  1. derives evidence one test at a time;
  2. runs the guarded `investigate` step (writer `investigator`), which writes write-once
     verdicts;
  3. closes the gateway `completed`.
- No step runs between the verdicts and the close.
- `step_bound` counts plan tasks, derivations, and the verdict step only.
- The execution graph's `supervisor` writer is unbound (only `seed_plan` uses it).

**Verdicts (`packages/investigation/verdicts.py`).**
- `evaluate_hypothesis` reads only `plan.tasks` and the hypothesis's own `tests`.
- The state invariant requires `verdict.test_task_ids == hypothesis.tests`.
- `observe` supports equality keys only: no strata or standardization.

**Derived evidence.**
- `DerivedOperationName` is `rate_difference | mean_difference`.
- `DerivedEvidencePayload` holds two `DerivedRatio`s and their exact difference.

**Gateway.**
- Model calls exist only inside task attempts (`research_documents`, `prepare_sql`,
  `prepare_correction`).
- Every model call goes through `_model_admission` and `_AdmittedLLMClient`, and is settled by
  `_settle`.
- Owner-only operations: `resource_status`, `skip_task`, `finish_attempt`, `derive_evidence`,
  and `close`.
- A task with `evidence_refs` and no trusted policy criteria is `policy_refused` (the R4
  deferral).

**Churn data.**
- The oracle has `smb_basic_tenure` (new is fewer than 6 months):
  - Q1: new 15/57 and established 15/443;
  - Q2: new 12/52 and established 58/448.
- It also has `q2_tenure_standardized_rate_using_q1_weights` 0.1410 and
  `tenure_standardized_rate_change` 0.0810. Standardization is defined as descriptive direct
  standardization with Q1 weights, "not a causal attribution".
- The draft board memo claims Enterprise churn doubled. The data has it flat at 14/700 in both
  quarters, and H4 is `refuted`.
- The INV-6 churn v2 plan has no memo task.
- `tenure_months_start` is an integer column with no NULLs in the at-risk rows.

**Prompts.** The INV-6 v2 planning prompt is pinned by tests and not registered in
`evals/v1/prompt-registry.json`. The SQL and RAG prompt builders are registered and must not change.

## Decisions (recorded in S0; C1 to C9)

| ID | Decision |
|---|---|
| C1 | **Composite Critic.** Deterministic checks are authoritative and decide every finding about numbers, references, causation, document contradiction, conflicting or insufficient evidence, and coverage of completed work. A bounded model review may only *propose* coverage gaps (missing stratification). Each proposal is schema-validated and then deterministically confirmed: the target is an approved, completed SQL rate test; the field is a confirmed numeric schema field that is not already a key; the gap was not addressed before. An unconfirmed proposal is counted and dropped. Model text never becomes control flow, and the model cannot withdraw or override a deterministic finding. |
| C2 | **Transient claim draft, only because structured state cannot carry the defects (modified at review).** See "C2 necessity" below. After the verdicts of a round, the Business Investigator's call drafts typed, untrusted claims (`FindingsDraft`). The call is strictly bounded, gateway-metered, and owner-only. Each claim gives a hypothesis, the approved test, which number (`measure`, `reference`, `effect`), a value, a direction, `association` or `causation`, a basis (`data` or `document`), and cited evidence IDs. Claims stay in process for one critique: they never enter state, checkpoints, or reports, and a finding keeps only a claim's index. A draft that fails (refused before running, malformed, or `unavailable`) removes only the claim checks. Every check over verdicts and evidence (conflicting evidence, incomplete coverage, confirmation of model gaps) still runs. INV-8 owns the verified report. |
| C3 | **Separate authorization per follow-up round.** Follow-ups are new executable work outside the plan digest, so decision 3 needs an explicit authorization for each round. INV-7 defines a content-free binding and a trusted `FollowUpAuthorizer` port: scope, plan digest, round, a digest of the exact follow-up tasks, and a deadline no later than the plan approval's. Production code never constructs a `FollowUpApproval`, and production has no authorizer, so production fails closed: no round is admitted, the run ends normally, and the limitation `follow_up_not_authorized` is recorded. The asynchronous human approval flow (pausing, the UI, approver identity, and audit) belongs to INV-10 (#143). |
| C4 | **One follow-up kind: a stratified re-test.** A follow-up is a `hypothesis_test` with method `stratified_comparison` that copies an approved SQL rate test's resolved metric, filters, period, and claim, and adds a `Stratification`: a confirmed integer or number field and 1 to 3 ascending integer cut points, giving bands `band_0` to `band_n`. Evaluation is direct standardization with baseline weights: standardized comparison rate = Σ wᵦ·rateᵦ(comparison), with wᵦ = baseline populationᵦ / baseline population. For `increase` the effect is standardized minus crude baseline; `decrease` is the reverse; `higher_than` and mean metrics are refused in INV-7. Every band must meet `min_denominator`. The Supervisor constructs the task deterministically from a confirmed finding; a model never authors a task. Other follow-up kinds (document re-queries, new SQL objectives) are refused, and so is a request that would re-run a task that failed for a permanent reason. A critique never creates a retry. |
| C5 | **Rounds.** Critique 0 follows the first verdicts. Follow-up round 1 (if confirmed, valid, and authorized) leads to critique 1, and that can lead to follow-up round 2. Critic rounds and replan rounds are reserved and settled on the execution ledger and never reset. No third follow-up round can exist: the ledger, `validate_follow_ups`, a state invariant, and the owner each refuse it. Results of round 2 are re-evaluated deterministically but not critiqued again (`critique_rounds_exhausted`). A gap that an earlier round addressed is never requested again, so a model that keeps asking cannot extend the loop. |
| C6 | **Verdicts.** The `verdicts` channel stays write-once and is written once, after the last round, before the normal close (INV-6 rule). Each critique record keeps a content-free snapshot of that round's verdicts, so the reassessment is visible. An admitted follow-up test is an additional required test of its hypothesis: all tests must support, any refutation refutes, and a follow-up that did not complete makes the hypothesis `untestable/test_not_completed`. Re-evaluation can only use more verified evidence, never upgrade on model text, and never express causation. |
| C7 | **State (additive; `STATE_VERSION` stays 1).** The additions are: <br>• a `critiques` channel, an append-only list of `CritiqueRecord` with a new writer `critic`; <br>• a `follow_up_rounds` channel, an append-only list of `FollowUpRoundRecord` owned by `supervisor`; <br>• admitted follow-ups typed as `FollowUpTask`; <br>• `GatewayTool` values `draft_findings`, `review_findings`, and `open_follow_up_round`; <br>• `DerivedOperationName` `standardized_rate_difference`; <br>• the refusal code `follow_up_not_authorized` (task severity). <br>Every pre-INV-7 checkpoint restores unchanged; an older reader refuses the new vocabulary (`unknown_channel` or `invalid_value`). |
| C8 | **R4 stays deferred.** Follow-up tasks carry no `evidence_refs`; their justification is the critique record's finding. Their SQL therefore runs `not_applicable`, as INV-6 tests do. Document-derived policy criteria move to the first milestone that admits evidence-citing SQL. ADR Amendment 5 records this. |
| C9 | **Model context.** The draft prompt contains verified observations (counts, rates, effects from derived evidence), verdict codes, approved hypothesis statements, and verified document citations with their untrusted answer text, labelled as untrusted. The Critic prompt contains only the plan's hypotheses and test specifications, schema field names and types, verdict and finding codes, and already-addressed gaps: no rows, passages, document text, or draft text. Document text (including the memo's injected sentence) can therefore influence only claims, which are verified, and never a follow-up request. |

## Review outcome (2026-10-20)

- **C1 approved.** Deterministic verification is the authority. Model suggestions may only
  identify potential coverage gaps. The memo contradiction is assessed only against document
  evidence from an approved plan task whose citations verify (`document_citations_valid`). It
  must agree with an approved test's data observation.
- **C2 modified,** as recorded in the table above and justified below.
- **C3 approved.** A follow-up approval is bound to the exact task set (`follow_up_digest`).
  Scripted authorizers exist only under `tests/`; a structural test refuses any production
  construction of `FollowUpApproval`. An unauthorized round is reported as the limitation
  `follow_up_not_authorized`, never as complete verification. The run's status is still decided
  by its tasks, and the report states the limitation.
- **C4 conditionally approved.** Follow-ups are stratified rate re-tests only. The baseline-weighted
  direct standardization is recomputed with exact `Fraction` arithmetic from the integer counts of
  the verified stratified query payload:
  - weights from the baseline band populations;
  - every band's denominators checked against `min_denominator`;
  - any stated rate column checked within the 0.0001 tolerance.

  No expected value appears in production code. The follow-up result is new derived evidence: the
  original test's query and derived evidence are never changed or replaced, and the hypothesis
  verdict cites both.

**C2 necessity (why structured state alone cannot supply the claims).**
- **Wrong numbers.**
  - Verdicts and derived evidence are recomputed deterministically, so they cannot be wrong.
  - State holds no number at all.
  - The only model-authored numbers are SQL rate columns, already checked by the evaluator
    (`evidence_inconsistent`), and document answer prose.
  - Parsing numbers out of untrusted prose would make document text a control input.
- **Causal overreach.** It is unrepresentable in the structured state:
  - `Hypothesis.relationship` has the single value `association`;
  - verdict codes cannot express causation;
  - statements are display-only.
- **Memo contradiction.** The memo's claim exists only as document text. To compare it with data,
  something must state it as a typed direction for a typed test, and only a model can do that
  without parsing prose deterministically.

So the mandatory defects need model-authored claims. The draft is that transient, bounded input.
The checks never trust it: every value, direction, relationship, and reference in it is verified
against state and payloads.

**Achievability through genuine execution paths (checked against the code).** Every case runs
through the real execution owner, gateway, specialists, real SQLite churn data, and the real
four-PDF index, under scripted bounded adapters.

| Case | Path |
|---|---|
| Wrong number | The scripted draft states SMB Basic 2026-Q2 churn 0.18, citing `h1_rate`'s derived evidence. The Critic recomputes 70/500 = 0.14 from the verified payload, giving `numeric_mismatch` with that derived ID. |
| Causal overreach | A scripted draft claim with `relationship: causation` for H1 gives `causal_overreach`, `downgrade_to_association`, citing H1's derived evidence. |
| Memo contradiction | The INV-7 churn plan adds one approved `document_research` task, `memo` (purpose `contradiction_check`), as context for H4. It runs through Document Research on the real index, and its citation must verify. The scripted draft states, on a `document` basis citing that evidence, that Enterprise churn increased. H4's derived effect is 0, giving `document_contradiction` with the memo evidence and `h4_rate`'s derived evidence. If the citation does not resolve to a verified memo source, the test fails rather than passing on an unverified document. |
| Missing tenure stratification | The scripted Critic review proposes `{h1, h1_rate, tenure_months_start, [6]}`. The deterministic confirmation checks that `h1_rate` is a completed, approved rate test, that the field is a confirmed integer schema field and not already a key, and that the gap was not addressed before. The result is `missing_stratification` with origin `model_confirmed`. |
| Exactly one follow-up | Critique 0 confirms the tenure gap. The Supervisor builds one `StratifiedTestSpec` task; validation and the scripted authorizer bind it; the dispatcher runs it through the SQL Analyst (scripted stratified SQL, real execution). Critique 1's scripted review repeats the same gap (refused as already addressed) and proposes nothing new, so the run ends with `replan: none_needed`, one round record, and `replan_rounds == 1`. |

## Component boundaries

| Component | May | Never |
|---|---|---|
| Critic (`src/investigation/critic.py`) | Read the approved plan, the validated state view, verified typed payloads, the parsed draft, the parsed model review, and prior critique records that the owner hands it; return `CritiqueProposal`s | Hold or call the gateway, port, ledger, authority, model, SQL, retrieval, or resolver; create or admit tasks; write state; change budgets, approvals, or policy flags |
| Business Investigator | As INV-6; additionally name the claims a draft must cover (no change in authority) | Call the gateway or a model directly |
| Supervisor replanner (`src/investigation/supervisor.py`, new class `FollowUpPlanner`) | Turn confirmed `follow_up` findings into `FollowUpTask`s deterministically | Approve, schedule, or run tasks; widen fields, filters, periods, or thresholds |
| Run owner (`investigation_execution.py`) | Call the three new owner-only gateway operations; validate critique proposals (`validate_critique`) and follow-ups (`validate_follow_ups`, proposer Critic); ask the trusted `FollowUpAuthorizer`; write through guarded `critique`, `replan`, and `dispatch` steps | Construct a `FollowUpApproval` or authority; admit an unvalidated, unauthorized, or out-of-round task |
| Gateway | Run the bounded draft and review model calls, reserve rounds, verify follow-up approvals for every follow-up task operation | Accept caller text, numbers, tasks, or approvals it did not verify |

## Contracts (S1; `packages/investigation/critique.py`, `hypotheses.py`, `evidence.py`, `state.py`)

- **`Stratification`:** `field`, `cut_points` (1 to 3 strictly ascending integers in
  [−10⁶, 10⁶]). Band labels are fixed (`band_0` to `band_n`); `band_i` holds values in
  `[cut_{i-1}, cut_i)`.
- **`StratifiedTestSpec(HypothesisTestSpec)`:**
  - method `stratified_comparison`, and `stratification` required;
  - metric, period, and claim must be explicit, because follow-ups do not depend on plan defaults.
- **`FollowUpTask(PlanTask)`:** input is `StratifiedTestSpec` only; `max_attempts` 1; no
  dependencies on plan tasks other than its target test.
- **`FindingsDraft` / `Claim`** (untrusted, parsed strictly from model JSON):
  - at most 16 claims;
  - evidence IDs only pattern-checked, so fabricated IDs parse and are then flagged;
  - `statement` is display-only and never stored.
- **`CriticReview`** (untrusted): at most 3 `GapProposal`s, each `{hypothesis_id, test_task_id,
  field, cut_points}`.
- **`CritiqueFinding`** (state-safe):
  - a `code` (below), a fixed `severity` per code, and a `disposition`;
  - optional `hypothesis_id`, `claim_ref` (index only), and `task_id`;
  - `evidence_ids`, existing IDs only;
  - optional `stratification`, and `origin` `deterministic | model_confirmed`.
- **`CritiqueRecord`:**
  - `round` (0 or 1), `draft_status` and `review_status` (`valid | invalid | unavailable`);
  - `findings` (at most 24) and `rejected_proposals` (a count);
  - `verdicts` (a snapshot of `VerdictRecord`s) and a `replan` outcome: `none_needed`,
    `admitted`, `not_authorized`, `invalid`, `round_limit`, or `budget`.
- **`FollowUpRoundRecord`:** `round` (1 or 2), `critique_round`, `task_ids`, `follow_up_digest`,
  `finding_indexes`, and `approval: FollowUpApproval` (`follow_up_digest`, `round`, `approved_at`,
  `deadline`).
- **`DerivedEvidencePayload`** gains `strata` (per-band baseline and comparison counts) for
  `standardized_rate_difference`:
  - `reference` is the crude baseline;
  - `measure` is the reduced exact standardized rate;
  - the value is recomputed from the strata.

**Finding codes, severity, and disposition**

| Code | Source | Severity | Disposition |
|---|---|---|---|
| `numeric_mismatch` | claim value differs from the recomputed observation by more than 0.0001 | high | `correct_from_evidence` |
| `direction_mismatch` | claim direction disagrees with the effect sign | high | `correct_from_evidence` |
| `causal_overreach` | claim relationship is `causation` | high | `downgrade_to_association` |
| `document_contradiction` | a document-based claim with a verified citation disagrees with the data | high | `retain_data_verdict` |
| `unsupported_reference` | a cited ID is absent from this investigation (fabricated or another scope) | high | `reject_claim` |
| `evidence_not_bound` | a cited ID exists but belongs to another hypothesis's tasks | medium | `reject_claim` |
| `document_evidence_unverified` | a document claim's citation is missing or unverified | medium | `reject_claim` |
| `insufficient_evidence` | a claim asserts a direction or value for an untestable hypothesis | medium | `retain_limitation` |
| `conflicting_evidence` | a verdict reason `conflicting_evidence` | medium | `retain_limitation` |
| `incomplete_coverage` | a testable hypothesis whose test did not complete (with a permanent category), or with no claim | low | `accept_limitation` |
| `missing_stratification` | a model-proposed gap that was deterministically confirmed | medium | `follow_up` |

`validate_critique(proposal, inputs)` recomputes the deterministic findings from scratch and
refuses any proposal that is not exactly the set the inputs give. The owner writes only validated
records.

## State transitions and LangGraph topology (S4)

```text
start → dispatch* ──FINISH_RUN──► derive* (dispatch) ──► draft_findings (dispatch, model)
                                                   └──► review_findings (dispatch, model, critic round)
      ──► critique (writer critic: CritiqueRecord n)
      ──► [follow-up admitted?] open_follow_up_round (dispatch, replan round, approval re-verified)
            ──► replan (writer supervisor: admitted_follow_ups + FollowUpRoundRecord)
            ──► register (dispatch: follow-up tasks pending) ──► dispatch* (follow-up tasks) …
      ──► [no follow-up] investigate (writer investigator: final verdicts) ──► dispatch (close completed)
      ──► finish ──► terminal
```

- Every step is a `guarded_node` through `InvestigationGraphBuilder`.
- `critique` and `replan` are new steps with conditional edges from `dispatch`. Only the owner's
  routing decides which runs.
- `step_bound` adds per round: derivations, two review calls, the critique, the round opening,
  the replan, registration, and every follow-up task's worst-case operations.
- Cancellation is checked before every dispatch action and inside `critique` and `replan`, which
  write nothing once cancelled.
- A draft or review result that arrives after cancellation is discarded by the gateway's post-call
  check (`cancelled`, investigation-terminal), so no critique is written.

## Budget, approval, and failure semantics

- **Draft and review model calls:** each is one bounded model reservation. The review also
  reserves and settles `critic_rounds=1`; opening a round reserves and settles `replan_rounds=1`.
  The owner never calls past the limits, so a ledger refusal here is a defect or a race and is
  terminal (`budget_exhausted`).
- **Before a round:** plan approval unexpired, live authority, and the authorizer's approval
  bound to the exact follow-up digest. Otherwise no round is opened and the run ends normally
  (`follow_up_not_authorized`).
- **During a round:** every gateway operation on a follow-up task re-checks that its round's
  approval is present and unexpired. Otherwise the task is refused with `follow_up_not_authorized`
  (task severity), and the hypothesis becomes `untestable/test_not_completed`.
- **Malformed model output** (draft or review): status `invalid`, never retried. The
  deterministic findings that need no draft still run; no model gap means no follow-up.
- **Timeouts and retry exhaustion of follow-up tasks:** the INV-5 rules apply unchanged
  (`max_attempts` 1, so no retry).
- **Budget exhaustion during a follow-up:** `budget_exhausted`; no verdicts are written (INV-6
  rule), but the critique records keep the round-0 snapshot.
- **Production (no bounded adapter):** `metering_unbounded`, fail-closed as before.

## Security threat analysis

| Threat | Control |
|---|---|
| Prompt injection from documents (the memo) creates work | Document text reaches only the draft; the Critic prompt excludes it; follow-ups come only from confirmed gaps over approved tests and confirmed fields (C1, C9) |
| Model asks for more analysis forever | Two critic and two replan rounds on the ledger; addressed gaps cannot be requested again; at most 3 per round; validation, an invariant, and the owner each refuse a third round |
| Fabricated or cross-tenant evidence IDs | Every cited ID must exist in this investigation's validated view, whose evidence invariants enforce scope; unresolved IDs are never stored |
| Wrong numbers or causal language | Recomputed from verified payloads with exact arithmetic; `causation` is always a finding and never a verdict |
| Critic or Supervisor widens scope | Follow-ups copy the approved test's metric, filters, period, and claim; the stratification field must be in the confirmed schema; no other kinds; `validate_follow_ups` with proposer Critic |
| Unauthorized execution | Separate approval per round bound to the follow-up digest; checked by the owner and the gateway, on every follow-up task operation; no production authorizer |
| Critic mutates trusted state | The `critic` writer owns only `critiques`; `owned_update` refuses foreign channels; structural tests restrict the Critic module's imports and names |
| Content leakage | Checkpoints hold codes, IDs, digests, schema field names, and integer cut points only; no claims, values, statements, prompts, or model text; no telemetry or audit field is added |
| Late results after cancellation | The gateway's post-call cancellation check discards them; the critique and replan steps write nothing after cancellation |

## Test plan and acceptance mapping (S2 to S5)

| # | Criterion | Test file |
|---|---|---|
| T1 | The four seeded defects (wrong number, causal overreach, memo contradiction, missing tenure stratification) are flagged with the right code, severity, disposition, and evidence IDs | `tests/test_investigation_critic.py` (pure) and `tests/test_investigation_churn_critique.py` (end to end) |
| T2 | Churn: the original tasks complete; `missing_stratification` on `h1_rate` by `tenure_months_start` at 6; exactly one follow-up round; tenure-stratified SQL runs through the SQL Analyst; strata, standardized rate (0.1410), and change (0.0810) match the oracle; H1 is reassessed and stays `supported_association`, citing the standardized evidence; critique 1 requests nothing; status `completed` | `tests/test_investigation_churn_critique.py` |
| T3 | Round limits: round 1 admitted; round 2 only when confirmed and authorized; a third is impossible (owner, validator, invariant, ledger); exhausted budget, attempts, or authorization ends earlier; repeated model requests do not extend the loop | `tests/test_investigation_replanning.py` |
| T4 | Security: unauthorized follow-ups; cross-tenant and fabricated IDs; unconfirmed schema fields; document follow-ups; invalid dependencies and duplicate IDs; malformed critique and draft; Critic writing foreign channels; cancellation during review and replan; late results; budget exhaustion during follow-ups; repeated or conflicting proposals; missing source documents | `tests/test_investigation_replanning.py`, `tests/test_investigation_critique_state.py`, `tests/test_investigation_boundaries.py`, `tests/test_investigation_bypass.py` |
| T5 | Contracts: round-trip; strictness; v1/v2 plan digests unchanged; pre-INV-7 checkpoints restore; standardized derived evidence recomputes; tampering fails | `tests/test_investigation_critique_contracts.py`, `tests/test_investigation_stratified.py` |
| T6 | Regressions: every INV-3 to INV-6 suite, the legacy LangGraph suites, and evaluations stay green; INV-6 tests change only where they pin the step bound or the scripted adapter must answer the two new prompts | the existing suites |

## Expected files

**New:** `packages/investigation/critique.py`, `src/investigation/critic.py`,
`src/orchestration/review_prompts.py`, this brief, and the tests listed above, plus
`tests/investigation_critique_fakes.py`.

**Modified:**
- `packages/investigation/`: `hypotheses.py` (stratification and follow-up spec), `verdicts.py`
  (stratified observation; follow-ups bound to hypotheses), `evidence.py` (strata), `state.py`
  (channels, writer, invariants), `validation.py` (stratified follow-ups), `refusals.py`,
  `dispatch.py` (limitation codes), `interim_report.py` (critique and round rows), and
  `__init__.py`;
- `src/investigation/supervisor.py` (`FollowUpPlanner`);
- `src/orchestration/tool_gateway.py` (three owner-only operations, the follow-up approval check,
  the stratified question);
- `src/orchestration/investigation_execution.py`;
- `tests/investigation_execution_fakes.py` (`RoutedAdapter` answers review prompts);
- the INV-6 step-bound pin;
- `docs/adr/0024-multi-agent-investigation.md` (Amendment 5) and
  `docs/architecture/investigation-workflow.md`.

**Do not touch:** `apps/**`, OpenAPI, the web client, `evals/**` (baselines, oracle, reference SQL,
registry), `sample_data/**`, the registered SQL and RAG prompt builders, the question graph,
execution limits, telemetry and audit allowlists, dependency and lock files, and containers.

## Stages

| Stage | Scope | Exit |
|---|---|---|
| S0 | This brief | Reviewed |
| S1 | Contracts and state vocabulary, stratified spec and derived payload, invariants | Contract, restore, and digest tests; `check_all` |
| S2 | Pure Critic checks, `validate_critique`, stratified evaluation, follow-up validation | Seeded-defect and adversarial unit tests |
| S3 | Gateway operations, review prompts (versioned, pinned), follow-up approval checks | Gateway and bypass tests |
| S4 | Supervisor replanner, Critic agent, owner and graph integration | Lifecycle and round-limit tests |
| S5 | Churn acceptance and security end to end | The acceptance matrix |
| S6 | ADR Amendment 5, workflow doc, readiness | `check_all` green; evaluations unchanged |

## Risks and limitations

- **No real human approval loop.** The authorizer is a synchronous trusted port: production has
  none, so follow-ups never run in production until INV-10 provides an approval flow.
- **Unregistered prompts.** The draft and review prompts are versioned and pinned by tests but not
  registered in `evals/v1/prompt-registry.json`, like the INV-6 planning prompt.
- **Coverage gaps depend on the model.** They are detected only when the model proposes them; the
  deterministic part confirms them but cannot discover them. Live recall is an evaluation question
  (#145, #147).
- **Standardization is descriptive** (as the oracle defines it) and only for rate metrics. NULL
  stratification values are excluded by the rendered question, with no separate check.
- **Conservative downgrade.** A failed follow-up downgrades a supported hypothesis to
  `untestable`, the safe direction.
- **Process-local limits remain.** Checkpoints and replay protection are process-local, as in
  INV-5 and INV-6.
