# Brief: #147 EVAL-2 live-model comparison of the investigation and bounded workflows

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/147
Tier: strongest. Check mode: `--full` once code changes (it touches the API test harness).

**Status (2026-11-17):** decisions E1 to E4 approved as recommended (E2: the confound is accepted
and labelled). No harness code yet. The live run (S4) is blocked on PROD-1 S6 (#198) and a separate
spend approval; external spend is $0.

## Outcome

A reproducible comparison of the existing bounded workflow (arm A) and the multi-agent investigation
workflow (arm B) on the same cases, graded by the same evaluator-only oracles, with sample sizes and
Wilson 95% intervals, labelled **measured live**, and never a CI gate. The same harness runs offline
against scripted adapters, so every grader and report path is tested before any spend.

## Facts (verified against `3f53d70` on 2026-11-17)

- **Cases.** `evals/investigation/v1/public_cases.json` holds the churn cases (no expected values);
  `oracle.json` and `reference.sql` are evaluator-only and read only after a run returns.
  `trajectory_cases.json` grades `churn-full-investigation` and the three retail investigation cases
  (`investigation-top-region-fulfillment`, `investigation-review-category-rate`,
  `investigation-october-peak-explanation`) on scripted runs of the real graph (EVAL-1).
- **Graders to reuse.** `packages/evaluation/investigation_trajectory.py` (citation, grounding, critic,
  recovery, budget, report, delegation blocks; `causal_markers_found`; `sql_equivalence`),
  `packages/evaluation/workflow_baseline.py:wilson_interval`, and the advisory judge in
  `packages/evaluation/judge.py` (rubric 1.0.0: groundedness, completeness, relevance; calibrated
  against `evals/judge/v1/labelled.json`).
- **Live tooling.** `scripts/run_live_demo_eval.py` already runs the bounded workflow against Gemini
  under explicit call and token limits (free-tier mode included). `scripts/prod1_live_smoke.py`
  (PR #221) is the only tool allowed to send the bounded OpenAI adapter's requests.
- **Admission.** Arm B can run on a real model only with a reviewed OpenAI bundle
  (`LIVE_ADMISSION_RELEASED`, PROD-1 S6). Arm A runs on the bounded workflow's provider chain
  (Gemini by default).

## Decisions (approved 2026-11-17)

- **E1 Case set (approved: the issue's four).** `churn-full-investigation` plus the three retail
  investigation cases, which EVAL-1 already measures, so the scripted baseline is directly
  comparable. The other churn cases (contradiction, unsupported causation, insufficient evidence,
  injection) are added as **safety probes** that are reported, not compared.
- **E2 Model confound (approved: accept and label it).** Arm A runs on Gemini; arm B can only use
  the reviewed OpenAI snapshot. A difference then measures *workflow and model together*, and the
  report says so in its headline. Alternatives: (b) a bounded-workflow OpenAI client: new
  production code, a new hosted recipient, and re-consent; out of scope here. (c) Run arm A twice,
  on Gemini and on OpenAI once (b) exists. Either way, no claim attributes a difference to the
  workflow alone.
- **E3 Sample size (approved: 5 repetitions).** Five runs per case per arm: 20 runs per arm, so
  each dimension's Wilson interval rests on n = 20 (or fewer where a dimension does not apply,
  stated). That is enough to report rates with honest intervals, not to claim significance; the
  report makes no significance claim without a test. The judge runs once per answer.
- **E4 Approvals inside arm B (approved: an evaluation approver).** Plan approval and SQL waves
  need a person. The harness approves exactly the plan and waves it is shown, through the real
  approval endpoints, as a declared evaluation-only operator, and records every decision. It never
  edits SQL. A run whose plan is invalid or rejected is a graded outcome, not a retry.

## Metrics (per run, then rates with Wilson 95%)

| Dimension | Arm A | Arm B | Source |
|---|---|---|---|
| Task completion | answered, refused, failed | terminal status | API outcome |
| Sub-question completeness | required sub-questions answered | the same | oracle `case_expectations` |
| Numeric accuracy | stated numbers equal oracle facts within tolerance | the same | oracle `facts` |
| SQL result correctness | `sql_equivalence` against `reference_sql_results` | per task | trajectory grader |
| Citation coverage | cited passages present and valid | `citation_block` | trajectory grader |
| Unsupported or contradictory claims | forbidden claims present | the same | oracle |
| Causal-language violations | `causal_markers_found` | the same | trajectory grader |
| Model calls, tokens, latency | telemetry | ledger and telemetry | content-free records |
| Cost | estimated (price entry) and provider-reported where available | the same | labelled separately |
| Failure modes | fixed taxonomy | recovery block plus terminal category | harness |
| Judge (advisory) | rubric scores | the same | judge, never a gate |

## Implementation slices

| Slice | Content | Acceptance |
|---|---|---|
| S1 | Comparison runner over the real API for both arms with **scripted** adapters; graders above; report document and Markdown summary; oracle read only after each run | Deterministic report from scripted arms; oracle-isolation test; no live client constructible |
| S2 | Judge comparison mode (rubric version bump; calibration recorded) | Calibration on the labelled set reported; judge cannot fail anything |
| S3 | Live mode guards: explicit flags, key from the environment only, refusal under CI, call, token, and USD caps from the reviewed price entry, abort on uncertain outcomes (as in `prod1_live_smoke`) | Every refusal tested offline; plan printed with worst-case spend |
| S4 | The measured run (separate spend approval) and committed results | Results labelled measured live, with n, intervals, model identities, date, commit, and limitations |

## Do not

- Remove, demote, or soften any evaluation case to improve a figure; failures and refusals are
  reported with successes.
- Run anything live before PROD-1 S6 passes and the spend is approved.
- Make it a CI gate, or describe scripted results as live.
- Pass oracle content to either arm.
