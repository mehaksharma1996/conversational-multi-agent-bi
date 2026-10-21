# S0 implementation brief: #141 INV-8 verified investigation report composer and verifier

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/141
(part of umbrella #131; depends on #140 INV-7).
Tier: strongest. Check mode: `python -m scripts.check_all` (no API or web change, so not `--full`).

## Outcome

After INV-8, every terminal v2 investigation ends with one in-process `VerifiedReport`. The report
is built in the execution graph by two guarded steps, `compose_report` and `verify_report`.

- **Facts.** Every number comes from the deterministic fact sheet, which is recomputed from
  verified payloads with exact arithmetic.
- **Narrative.** A bounded, metered narrative call may phrase those facts. It cannot introduce a
  number, a citation, a verdict, a recommendation, or a causal claim the verifier does not accept.
- **Authority.** `verify_report` decides whether the narrative is accepted. When it is not, the
  report is the deterministic fallback, which needs no model call. The fallback is labelled as
  such and states why.

State keeps only a content-free `report_record`. The report itself stays in process until #144.

## Baseline (verified 2026-10-21)

- Base: `origin/main` at `b31991a619024ea171aa4a9a7b96de42189a4ca8` (merge of INV-7 PR #175, after
  #176). Main CI for that SHA was **in progress** when the worktree was created. Recheck it before
  S1 and before pushing.
- Branch `feat/iteration2-inv8-verified-reporting`, worktree
  `work/agent-worktrees/141-inv8-verified-reporting`. No other INV-8 branch or worktree existed.
  The primary checkout (`feat/iteration2-ui-stage1-workbench-shell`, untracked `.archify/` and
  `repository-audit-for-chatgpt.md`) is untouched.

## Facts (verified against the code on 2026-10-21)

**Report today.** `packages/investigation/interim_report.py` is pure and deterministic and makes no
model call.
- `compose_interim_report(view, payloads)` re-verifies each payload (`verify_payload`) and emits:
  - task rows;
  - query tables (verified cells, at most 50 rows);
  - citation *locations* (filename and page, only when `document_citations_valid`);
  - verdict rows (codes and IDs);
  - critique rows (codes);
  - limitations as fixed `LimitationCode`s with reviewed templates.
- It is labelled `"interim, unverified, association only"`. A `failed` report carries codes only
  (R8).
- Nothing in the execution graph calls it today; tests call it after the run.

**Authoritative numbers.**
- `DerivedEvidencePayload` (`evidence.py`) holds exact `DerivedRatio`s for `rate_difference`,
  `mean_difference`, and `standardized_rate_difference` (with `strata`). It is recomputed from the
  typed query payload by the gateway's `_derive`.
- `verdicts.observe` / `evaluate_plan` recompute `TestObservation`s with `Fraction` arithmetic.
- `NUMERIC_TOLERANCE` and `RATE_COLUMN_TOLERANCE` are both 1/10,000.
- State holds no numbers.

**Verdicts and critic findings.**
- `VerdictRecord`: hypothesis ID, `supported_association | untestable | refuted`, fixed reasons,
  evidence IDs, and test task IDs. It is write-once, after the last round.
- `CritiqueRecord`: per round, `CritiqueFinding`s with fixed code, severity, and disposition, a
  verdict snapshot, and a `ReplanOutcome`.
- `_critique_limitations` already maps these to `critique_unavailable`,
  `follow_up_not_authorized`, `follow_up_invalid`, `replan_limit_reached`, and
  `critique_rounds_exhausted`.

**Evidence resolution.**
- `view.evidence` holds content-free `EvidenceRecord`s with kind, task, attempt, and provenance;
  scope is enforced by state invariants and `check_binding`.
- Payloads exist only in the gateway (`evidence_payloads(view)`); each is bound to its record by
  `verify_payload`.
- Document citations count only when `document_citations_valid`. Their location must equal the
  record's provenance.

**Model calls.**
- Only inside `InvestigationToolGateway`, through `_model_admission` (exact `BoundedModelAdapter`
  envelope) → `_reserve` → `_AdmittedLLMClient` → `_settle`.
- INV-7's `_review_call` is the reusable shape: one call, `invalid_input` when over the cap,
  failures as categories, and a late answer discarded on closure or cancellation.
- Production has no admissible adapter (`metering_unbounded`).

**Execution owner** (`src/orchestration/investigation_execution.py`).
- Order: `dispatch*` → derivations → INV-7 review rounds → `investigate` (verdicts) → `dispatch`
  closes the gateway `completed` → `finish` (status) → `terminal`.
- The gateway is closed before `finish`, so any report-model call must run before the close.
- `step_bound` is explicit.
- `ExecutionResult` returns `state` and `payloads`, with no report.

**Prompt registry.**
- `evals/v1/prompt-registry.json` hashes five builder functions.
- `tests/test_prompt_registry.py` forces registration only of functions named `build_*prompt` under
  `src/`.
- Not registered: the INV-6 planning prompt (its text is a module constant,
  `planning_prompt.py`), and INV-7's `render_findings_prompt` and `render_critic_prompt` (pinned
  by tests only).
- The offline evaluation's `prompt_fingerprints` (`packages/evaluation/runner.py`) is a separate,
  fixed list of five. Adding registry entries does not change any evaluation baseline.

**Playbook rules.**
- No approved playbook-rule registry exists anywhere in the repository.
- The retention playbook is a PDF (document evidence). Its hypothesis H3 ("playbook reduced
  churn") is `untestable/required_data_absent`.

**Churn oracle** (read by tests only).
- SMB Basic 30/500 = 0.0600 → 70/500 = 0.1400 (+0.0800).
- Tenure-standardized Q2 rate 0.1410; change 0.0810.
- Overall 0.0480 → 0.0710.
- Verdicts: H1 supported, H2 supported, H3 untestable, H4 refuted.

## Decisions for review (V1 to V10)

| ID | Decision |
|---|---|
| V1 | **Extend, do not duplicate.** A new pure module `packages/investigation/report.py` holds `VerifiedReport` (schema v1) and `compose_fact_sheet`, `compose_fallback`, and `verify_report`. The evidence body *reuses* the interim composer: `compose_interim_report` runs first, and its rows (tasks, query tables, citations, verdicts, critiques, limitations) are carried into the verified report unchanged. `InterimReport` and its tests are untouched. |
| V2 | **Fact sheet is the single source of numbers.** Each `ReportFact` (`f1`…) is computed from a verified `DerivedEvidencePayload` and its source query evidence. It carries: metric (event, population), unit (`proportion`), filters and period values from the approved spec, baseline and comparison values, absolute difference (shown also as percentage points), relative change (only when the baseline is > 0), direction, standardization label, precision, and evidence IDs. Facts are held as exact `Fraction`s and rendered by one formatter: 4 decimals for proportions, 1 decimal for percent and percentage points, half-up. The verifier recomputes the sheet from state and payloads and requires equality. |
| V3 | **Narrative is structured, not prose.** The model returns strict JSON: sections → sentences. Each sentence has a `kind` (`summary`, `finding`, `hypothesis`, `critique`, `limitation`, `recommendation`), typed references (`fact_ids`, `hypothesis_ids`, `limitation_codes`, `recommendation_ids`, `task_ids`), typed assertions (`verdict`, `direction`, `task_status`, `relationship` limited to `association`), and short `text`. **Text may contain no digits**: numbers appear only as placeholders such as `{f1.change}`, which the composer renders from the fact sheet. Citations are not written by the model; they are derived from the referenced facts. |
| V4 | **Causal gate in three layers.** (1) `relationship` can only be `association`; a `causation` value parses and is rejected (`causal_claim`). (2) Each sentence's assertions are checked against its references: a `finding` must reference facts, and a `hypothesis` sentence's verdict must equal the record. (3) A reviewed lexicon gate for causal or efficacy wording ("caused", "led to", "drove", "because of", "due to", "resulted in", "proves", "effective", and similar). The lexicon is defence in depth, not the primary control. The fallback states `causation_not_established` unconditionally. |
| V5 | **Recommendations come from a reviewed rule catalogue.** No approved playbook-rule registry exists, so INV-8 adds `RECOMMENDATION_RULES` (versioned `inv8-recommendation-rules-v1`, in code, reviewed). Each rule has a fixed ID, a trigger over verdicts, facts, findings, or limitations, and fixed wording that proposes *evaluation or data collection*, never an established intervention. Examples: supported association → "evaluate retention actions for the segment in a controlled test"; refuted document claim → "correct the document before circulation"; required data absent → "collect the data needed to test it". Each instance cites its triggering facts and evidence. The narrative may only reference recommendation IDs present in the report. An unknown rule ID is `playbook_rule_unknown`. Document passages (including the retention playbook) are never rules. |
| V6 | **Hypothesis descriptions are rendered from the typed spec**, not taken from the approved statement. Amendment 4 decision 4 keeps statements out of reports. Example: "rate of churned over at-risk customers, plan = SMB Basic, 2026-Q1 → 2026-Q2, claimed increase". |
| V7 | **Graph placement.** After `investigate` (the final verdicts) and before the gateway closes, the owner makes at most one narrative call. It is a new owner-only gateway operation, `compose_narrative` (`GatewayTool` addition), with one bounded reservation, rendered from the fact sheet only. The gateway closes `completed`, and `finish` sets the status. Two new guarded steps then run before `terminal`: `compose_report` (in process only; writes nothing but its writer marker) and `verify_report` (writer `reporter`, the only writer of the new write-once `report_record` channel). `step_bound` adds 3. There is no loop: a failed verification never triggers a second call. |
| V8 | **Lifecycle.** The narrative call is made only for `completed` or `completed_partial`, when the ledger has room for the full reservation (checked by the owner before the call) and an admissible model exists. Otherwise there is no call, and the fallback reason is one of `narrative_not_attempted_status`, `narrative_budget_unavailable`, or `narrative_model_unavailable`. A provider failure or timeout is `narrative_unavailable`. Malformed output or a refusal is `narrative_invalid`. A late answer after cancellation is discarded by the gateway (`cancelled` is investigation-terminal, as in INV-7). A ledger refusal despite the pre-check is a race and keeps the existing terminal `budget_exhausted` semantics. The report's status is always the run's terminal status: a cancelled, failed, or exhausted run is never reported as completed. A `failed` run gets a codes-only report (R8). |
| V9 | **Storage.** The `VerifiedReport` is returned in `ExecutionResult.report` (in process). The checkpointed `report_record` holds only the report schema version, `mode` (`model_assisted`, `deterministic_fallback`, or `codes_only`), `verification` (`passed`), up to 8 fixed reason codes, counts, and the SHA-256 of the report's canonical JSON. It holds no text, numbers, or claims. If the fallback itself fails verification (a defect), `report_record.mode` is `unavailable` with reason `fallback_failed_verification`, no report is returned, and the run status is unchanged. `STATE_VERSION` stays 1 (an additive channel; older readers refuse it). |
| V10 | **Prompts and the registry.** The narrative prompt is `inv8-narrative-prompt-v1` (`src/orchestration/report_prompt.py:render_narrative_prompt`). Add **three** registry entries: INV-7's `render_findings_prompt` and `render_critic_prompt`, and INV-8's `render_narrative_prompt`. Each is approved "user-directed INV-8 engineering work; pending maintainer review". Extend the discovery test to `render_*prompt` functions under `src/orchestration/`, *excluding* the INV-6 planning renderers, whose text is a module constant the registry's function hash would not cover. That exclusion is explicit and documented, and stays deferred until the registry can hash constants. No existing hash or evaluation baseline changes. The narrative prompt also gets a pinned rendered-churn-prompt digest test. |

## Review outcome (2026-10-21)

The table above is the S0 proposal. These rulings supersede it where they differ.

- **V1 approved.** The verified report reuses the interim composer's verified rows and the existing
  evidence contracts. `InterimReport` is unchanged.
- **V2 approved.** The rules for the fact sheet:
  - **Values.** Exact `Fraction`s, displayed half-up: proportions and means to 4 decimals;
    percentage points (difference × 100) and relative change (difference ÷ baseline × 100) to
    1 decimal, both signed. Relative change is omitted when the baseline is 0, and percentage
    points exist for rates only.
  - **What becomes a fact.** A test whose observation is unusable, or that has no matching
    derived evidence, yields no fact. Its hypothesis row shows why (verdict reasons).
  - **Independent recomputation.** The composer reads the derived payload; the verifier
    recomputes each fact from the source *query* payload (`observe`), a separate path, and
    compares.
- **V3 approved with modification: finite claim types and reviewed templates only.** The model
  returns typed claims and never writes prose:
  - the claim types are `scope`, `fact`, `comparison`, `verdict`, `finding`, `task`, `limitation`,
    `recommendation`, and `no_recommendation`;
  - the model's only freedom is which claims to include, their order, and their section;
  - every sentence is rendered by `packages/investigation/report.py` from a reviewed template plus
    verified values;
  - unknown fields are refused, so free text cannot even be parsed.

  The verifier checks the structured claims *and* the final rendered text:
  - it re-renders every sentence and requires exact equality (`rendered_text_mismatch`);
  - it masks approved data slots (fact values, IDs, approved filter values, descriptions), then
    rejects:
    - any remaining digit or number word ("doubled", "twice", "half", "percent", "one" …
      `numeric_unsupported`);
    - causal or efficacy wording (`causal_claim`);
    - comparative wording outside a `comparison` claim (`unsupported_comparison`).
- **V4 approved with modification.** The primary controls are structural:
  - the `relationship` field;
  - assertion-to-evidence checks for direction, verdict, comparison, and status;
  - the template-only output.

  The lexicon is supplementary only. Adversarial tests cover implied and paraphrased causation,
  numeric words, and unsupported comparisons. `causation_not_established` is a mandatory note in
  **every** report, codes-only included, and its approved wording is the only allowed occurrence
  of causal vocabulary.
- **V5 revised: investigation-method recommendations only.** `RECOMMENDATION_RULES`
  (`inv8-recommendation-rules-v1`) holds six method rules, each triggered by verified evidence or a
  stated limitation:

  | Rule | Trigger |
  |---|---|
  | `controlled_evaluation` | a supported association |
  | `collect_missing_data` | `required_data_absent` |
  | `complete_test` | an incomplete test |
  | `reconcile_document` | a `document_contradiction` finding |
  | `review_follow_up` | `follow_up_not_authorized` |
  | `rerun_investigation` | a cancelled, exhausted, or failed run |

  Any recommendation that touches a business intervention is labelled *unproven, requires
  validation* (note `intervention_unproven`). The catalogue is not a business playbook. If no rule
  triggers, the report states `no_eligible_recommendation`. A rule ID outside the catalogue is
  `playbook_rule_unknown`.
- **V6 approved.**
- **V7 approved conditionally.** The narrative call is made by the owner only after the final
  verdicts and before the gateway closes, and only for v2 plans (v1 runs get the deterministic
  report with reason `narrative_not_applicable`). Checks before the call:
  - the trusted cancellation, authority, and lifecycle checks of `_operate`;
  - the gateway itself refuses a second narrative call and refuses a status that is not eligible;
  - a no-charge readiness pre-check (model admissible, ledger headroom via the new pure
    `BudgetLedger.can_authorize`).

  There is no retry and no loop. The successful `model_assisted` path is exercised through the
  real graph in the churn acceptance test.
- **V8 revised.** The report has its own status fields:
  - `investigation_status`, always the run's terminal status;
  - `availability`: `complete` (completed), `partial` (completed_partial, cancelled,
    budget_exhausted), or `codes_only` (failed);
  - `verification`: `verified`;
  - `mode`: `model_assisted` or `deterministic_fallback`.

  How the narrative call is decided:
  - **Not attempted:** cancelled, failed, and exhausted runs never reach it, and no narrative
    work happens after a stop.
  - **Fallback reasons:** `narrative_not_applicable`, `narrative_not_eligible`,
    `narrative_disabled`, `narrative_model_unavailable`, `narrative_budget_unavailable`,
    `narrative_unavailable` (provider failure or timeout), `narrative_invalid` (malformed output or
    a refusal), and `narrative_rejected` (with its violation codes kept in the report, never
    suppressed).
  - **Errors:** a composition or verification exception is caught, so the report becomes
    `unavailable` in `report_record` and the run status is unchanged.
- **V9 approved.** Two write-once, content-free channels, each with its own writer (separation of
  duties):
  - `report_draft` (writer `reporter`): candidate mode, fallback reason, violation codes, and the
    candidate SHA-256;
  - `report_record` (writer `report_verifier`): verification outcome, final mode, availability,
    reason codes, counts, and the final SHA-256.

  Both may be written only after `finish` and before `terminal`. The report body stays in process;
  no persistent storage is added before INV-11.
- **V10 revised: no partial fingerprints.** The registry gains module targets
  (`module.path`, no `:qualname`), which hash the whole module's source.
  - `src.orchestration.review_prompts` (both INV-7 prompts) and the new
    `src.orchestration.report_prompt` are each self-contained: every instruction, schema, marker,
    and layout rule lives in the module. Values fed into them are data, as for the existing
    entries (policy doc, "data fed into a prompt").
  - The narrative prompt lists facts as structured fields formatted inside `report_prompt.py` and
    lists limitation and rule IDs by code, so the hash covers its whole construction.
  - The INV-6 planning prompt stays unregistered. Its gap is documented here and in ADR 0024
    Amendment 6. Its regression coverage (rendered-prompt pins in
    `tests/test_investigation_planning_prompt.py`) is unchanged. Follow-up: register
    `src.orchestration.planning_prompt` as a module target under its own reviewed change. Filing
    that as an issue needs the user's approval.
  - The evaluation runner's fingerprints are a separate list and are unchanged. (Corrected in
    S7: the registry file's digest *is* critical provenance of the v2 workflow baseline, so the
    baseline was updated by the documented procedure; see Readiness.)

## Report schema (S1, `packages/investigation/report.py`)

```text
VerifiedReport (frozen, extra=forbid; schema_version 1)
  status: ReportStatus                     # = run terminal status
  label: "verified, association only"
  mode: model_assisted | deterministic_fallback | codes_only
  fallback_reason: FallbackReason | None   # required iff mode != model_assisted
  executive_summary: tuple[Sentence, ...]  # ≥1 unless codes_only
  facts: tuple[ReportFact, ...]            # ≤ 2*MAX_PLAN_TASKS
  hypotheses: tuple[HypothesisRow, ...]    # id, description (V6), verdict, reasons,
                                           # tested|untested, test/follow-up task IDs,
                                           # supporting/conflicting evidence, fact IDs
  critic: tuple[ReportCritiqueRow, ...]    # reused; unresolved findings flagged
  recommendations: tuple[Recommendation, ...]  # rule_id, rule_version, fact/evidence IDs
  limitations: tuple[Limitation, ...]      # reused codes + INV-8 codes (below)
  evidence: tasks, query_tables, citations # reused interim rows (SQL / document / derived kept apart)
  narrative: tuple[Section, ...]           # rendered sentences (placeholders resolved), or ()
```

Each `Sentence` keeps its typed references, so the verifier can re-check a stored report and not
only the raw model output. New `LimitationCode`s:
- `causation_not_established` (always stated when any fact or hypothesis is reported);
- `observational_data_only`;
- `narrative_fallback` (with the fallback reason in the report, not the template);
- `hypothesis_untested`.

## Verifier (S3): `verify_report(report, inputs) -> tuple[ReportViolation, ...]`

The inputs are the authoritative validated view, the gateway payloads, and the revalidated
verdicts. The report is never an input to its own check. Fixed reason codes:

| Check | Reason code |
|---|---|
| A number in text (any digit), or a placeholder to an absent fact | `numeric_unsupported` |
| A fact differs from the recomputed fact sheet (value, unit, direction, period, or denominator) | `numeric_mismatch` |
| A citation or evidence ID absent from this investigation's evidence | `citation_unknown` |
| An ID present but from another scope or investigation, or not in the validated view | `citation_foreign` |
| An ID attributed to the wrong kind (SQL, document, or derived), task, or fact | `citation_misattributed` |
| `causation`, or causal or efficacy wording | `causal_claim` |
| A recommendation without a triggering fact or evidence, or a rule whose trigger does not hold | `recommendation_unsupported` |
| A rule ID or version not in the catalogue | `playbook_rule_unknown` |
| A hypothesis verdict differs from its `VerdictRecord` | `verdict_mismatch` |
| A mandatory limitation (run, critique, unauthorized follow-up, causation) is absent | `limitation_missing` |
| A sentence's direction or verdict contradicts its facts, or two sentences contradict each other | `claim_contradiction` |
| A task, follow-up, or critique described as completed when it was not | `task_status_misrepresented` |
| A section is missing, or the schema, status, or mode is inconsistent | `section_missing` / `report_inconsistent` |

Every reason code is a fixed literal and is safe to log. Verification never logs text.

## Fallback (S5)

`compose_fallback(inputs, reason)`:
- deterministic template sentences only (reviewed wording keyed by fact and verdict kind), with
  numbers rendered by the V2 formatter;
- every reused evidence row and every mandatory limitation;
- the recommendations from V5;
- `mode=deterministic_fallback` and `fallback_reason` set.

It must pass `verify_report` (asserted in tests and enforced at runtime, V9). Cancelled and
budget-exhausted runs get the fallback with their run limitation. A failed run gets `codes_only`.

## Model context for the narrative (V3/V7)

The prompt contains only:
- fact IDs with rendered values and units;
- spec-rendered hypothesis descriptions and their verdict codes;
- limitation codes with their reviewed wording;
- critique finding codes;
- recommendation IDs with their fixed wording;
- task IDs and statuses;
- the output schema.

It contains no rows, SQL, document text or answers, filenames, statements, scope identifiers,
budgets, or evaluator data. Document text therefore cannot reach the narrative call.

## Security threats

| Threat | Control |
|---|---|
| Model invents numbers or citations | No digits in text; placeholders resolved from the fact sheet; citations derived, never authored; full recomputation in `verify_report` |
| Prompt injection from documents | No document text in the narrative prompt; document evidence appears only as verified locations |
| Causal overreach | `association`-only field, assertion-to-reference checks, lexicon gate, unconditional causation limitation |
| Verdict upgrade or hidden limitations | Verdicts compared with `VerdictRecord`; mandatory limitation set recomputed and required |
| Foreign or stale evidence | Every ID resolved against the validated view of this scope (`check_binding` already binds the scope) |
| Unbounded or extra model calls | One reservation; owner pre-check; no retry; no loop; the gateway is closed after the narrative |
| Narrative path as a bypass | Owner-only gateway operation; structural test that only the owner calls it; not on `SpecialistGateway` |
| Content leakage | `report_record` holds codes, counts, and a digest; no telemetry or audit field is added |

## Acceptance mapping

| # | Criterion | Test file |
|---|---|---|
| A | 12 seeded violations, each rejected with its exact code | `tests/test_investigation_report_verifier.py` |
| B | Real scripted churn run (CSV and four indexed PDFs) gives a `model_assisted` report with every required section. Facts match the oracle (0.1400, 0.0800, 0.1410, 0.0810; overall 0.0480 → 0.0710 if a plan task measures it). The report covers H1/H2 supported, H3 untestable, H4 refuted, the tenure follow-up, the memo contradiction finding, `causation_not_established`, and recommendations | `tests/test_investigation_churn_report.py` |
| C | Invalid JSON, a digit or unsupported number, a fabricated citation, causal text, a timeout, a refusal, no budget, no model, and cancellation before composition each produce the fallback (or codes only) with the right reason and the unchanged run status | `tests/test_investigation_report_lifecycle.py` |
| D | Conformance: the compiled graph has every node and writer (Supervisor, the four specialists, critic, replan, `compose_report`, `verify_report`); the churn run executes each, shown by decisions and channels written | `tests/test_investigation_architecture_conformance.py` |
| E | Every INV-3 to INV-7 suite, unchanged except the step-bound pin and the `ExecutionResult` field addition | existing suites |

## Stages

| Stage | Scope |
|---|---|
| S0 | This brief; review of V1 to V10 |
| S1 | Contracts: `VerifiedReport`, facts, sentences, the rule catalogue, reason codes, the `report_record` channel and `reporter` writer, the `compose_narrative` tool literal |
| S2 | `compose_fact_sheet`, hypothesis rows, recommendations, mandatory limitations |
| S3 | `verify_report` and the 12 seeded-violation tests |
| S4 | `report_prompt.py`, the gateway `compose_narrative` operation, strict parsing |
| S5 | `compose_fallback` and its self-verification |
| S6 | Owner and graph integration, lifecycle, `step_bound`, `ExecutionResult.report` |
| S7 | Churn acceptance, conformance, registry entries (V10), ADR 0024 Amendment 6, the workflow document, readiness |

## Do not touch

`apps/**`, OpenAPI, the web client, `evals/**` baselines, oracle and reference SQL (the registry
JSON gains entries only, V10), `sample_data/**`, registered SQL and RAG builders, the question
graph, execution limits, telemetry and audit allowlists, dependency and lock files, containers,
the Streamlit isolation from #176, and INV-7 behaviour (except the extra steps after the verdicts).

## Risks and open points

- **No live evaluation.** Narrative quality and the lexicon's recall are untested on a real
  provider (#145, #147). Production stays fail-closed, so production reports are always the
  fallback.
- **The rule catalogue is new reviewed content.** Its wording needs maintainer review like a
  prompt.
- **Partial registry discovery.** Extending discovery to `render_*prompt` while excluding the
  planning renderers is a deliberate, documented gap.
- **Process-local report.** Persistence and display belong to #144.

## Readiness (S7, 2026-10-21)

Delivered on `feat/iteration2-inv8-verified-reporting`, one commit per stage after `b31991a`:

| Stage | Delivered |
|---|---|
| S0 | This brief, the review outcome |
| S1 | `VerifiedReport` contracts and typed claims; `report_draft` / `report_record` channels, `reporter` / `report_verifier` writers, `compose_narrative` tool |
| S2 | Fact sheet, hypothesis rows, method recommendations, notes (`compose_body`) |
| S3 | Reviewed templates and rendering; `verify_report` with the twelve seeded violations and V4 adversarial cases |
| S4 | `report_prompt.py` (`inv8-narrative-prompt-v1`), gateway `compose_narrative`, `narrative_readiness`, `BudgetLedger.can_authorize`, strict parsing |
| S5 | `report_composer.py`: candidate, fallback, acceptance |
| S6 | Owner and graph integration: `compose_report` and `verify_report` steps, `ExecutionResult.report`, step bound + 3 |
| S7 | Churn acceptance, architecture conformance, registry module targets, ADR 0024 Amendment 6, workflow document |

Results:
- **Seeded violations.** All twelve are rejected with their codes, alongside the V4 adversarial
  cases (implied and paraphrased causation, number words, unsupported comparisons).
- **Churn report.** It is model-assisted and verified through the real graph:
  - SMB Basic 0.0600 → 0.1400 (+0.0800, +8.0 pp, +133.3 %);
  - tenure-standardized Q2 0.1410, change +0.0810, matching the oracle within 0.0001;
  - H1/H2 supported, H3 untestable, H4 refuted;
  - memo citation and contradiction, tenure follow-up, every limitation, and method
    recommendations.
- **Failure paths.** Every narrative failure falls back with the run status unchanged. Cancelled
  and failed runs make no narrative call and never read as complete.
- **Conformance.** Every ADR 0024 component is a guarded node and acts in the churn run.
- **Regressions.**
  - Every INV-3 to INV-7 suite passes. The only edits to existing tests are the two step-bound
    pins (+3), the approved-schema pin (two channels, one tool), and the owner-only name lists.
  - The workflow baseline's review pin was updated with its provenance (below).
- **Workflow baseline.** The registry entries changed the registry file's digest, which the v2
  workflow baseline records as critical provenance. It was updated with
  `python -m tests.e2e.workflow_baseline_runner --update --justification ...`. Only that digest and
  the review record changed: 9/9 measured cases pass, 9 not measured, and all five safety
  invariants pass.
- **Checks.** `python -m scripts.check_all` passes 5/5 (ruff check, ruff format, mypy, pytest,
  evaluations). The OpenAPI compatibility check reports no breaking change, and `git diff --check`
  is clean.

Deviations and choices:
- The narrative call is v2-only. v1 runs get the fallback with `narrative_not_applicable`.
- `citation_unknown` covers fabricated and foreign IDs alike. A whole report from another
  investigation is `scope_mismatch`.
- Test fixtures bill one token per byte. The churn narrative prompt (about 5.4k bytes) needs a
  12k per-call cap in tests; over the cap the call is refused before it runs (`unavailable`), as
  designed.
- Follow-up: #185 registers the INV-6 planning prompt with a complete provenance fingerprint.

## Maintainer review corrections (PR #184, 2026-10-21)

- **`observational_data_only`.** The note now reads "Every measurement is an observational
  comparison of recorded data and shows association only." It no longer asserts that no
  controlled comparison was performed. `causation_not_established` remains a separate, mandatory
  note in every report.
- **`rerun_investigation` (rules v2).** The catalogue still has six rules.
  - When a documented blocker stopped the run, the recommendation carries `blocker` and cites it
    in `limitation_codes`. Its wording says to resolve the named restriction first and run again
    only after that, never while it remains.
  - A blocker is a security, authorization, policy, query-safety, resource, provider-configuration,
    or approval category. It is taken from the first investigation-terminal refusal's category,
    else, for a `failed` run, from the terminal record's (an earlier recoverable task failure is
    never the blocker of an owner-cancelled or exhausted run).
  - Cancellation, budget exhaustion, timeouts, and provider or internal failures keep the plain
    rerun.
  - The verifier rejects an unconditional rerun for a blocked run (`recommendation_unsupported`,
    `rendered_text_mismatch`).
- **Tests.** `tests/test_investigation_report_review_corrections.py`, including two real-graph
  runs (a mid-run authority revocation and an unadmitted provider).
- **Unchanged.** The narrative prompt module, the registry hashes, and the workflow baseline.

