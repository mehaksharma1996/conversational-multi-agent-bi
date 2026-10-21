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
