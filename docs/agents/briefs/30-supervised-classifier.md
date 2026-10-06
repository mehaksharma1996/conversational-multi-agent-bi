# Brief: #30 Deterministic supervised classifier

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/30
Tier: medium. Check mode: --full.

## Outcome

A confirmed binary label mapping can automatically produce a deterministic, leakage-aware classifier,
honest holdout metrics, a ranked review queue, a precision-recall chart, and clear responsible-AI
limitations across the API, report, memory, and React dashboard.

## Decisions

- Eligibility requires a confirmed `label` mapping, at least 100 labelled rows, exactly two classes,
  at least 20 rows in each class, and at least one usable non-identifier feature.
- Compare class-weighted logistic regression with a gradient-boosted tree using fixed random state,
  stratified training folds, and mean cross-validated PR-AUC. Keep a stratified holdout untouched for
  final metrics.
- Choose the classification threshold from out-of-fold training probabilities by maximum F1, with a
  deterministic tie break. PR-AUC is the primary selection and reporting metric.
- Exclude the label, profile-detected identifiers, constants, dates, and features that deterministically
  reproduce the target. Surface every exclusion without logging values or model internals.
- Refit the selected pipeline on all labelled rows only after holdout evaluation, solely to rank the
  review queue. Predictions are decision support, never automated decisions.

## Do

1. Add the supervised classification domain module and focused eligibility, determinism, metrics,
   leakage, and degenerate-label tests.
2. Wire it through capability detection and `build_analysis_bundle`.
3. Add an additive API response contract and serializers; regenerate OpenAPI and TypeScript types.
4. Add the precision-recall chart, business-report section, session-memory findings, and React dashboard
   review table.
5. Add a deterministic synthetic labelled fixture/demo and responsible-AI documentation.

## Do not touch

- Do not transmit labels, feature values, predictions, questions, or model internals in telemetry/audit.
- Do not treat accuracy as the primary metric or fit preprocessing/model selection on the holdout.
- Do not enable the classifier from an inferred-but-unconfirmed mapping.
- Do not represent scores as proof of fraud, eligibility, causation, or correctness.

## Acceptance

- [ ] Ineligible and degenerate datasets return a useful disabled report without raising.
- [ ] Repeated runs produce identical selection, metrics, curve, and ranked candidates.
- [ ] Reported metrics include precision, recall, F1, ROC-AUC, PR-AUC, positive rate, threshold,
      confusion matrix, and cross-validated PR-AUC.
- [ ] Identifier and deterministic target-proxy features never reach model training.
- [ ] API, report, memory, PR chart, dashboard, fixture, tests, and responsible-AI docs reflect results.
- [ ] `python -m scripts.check_all --full` passes.

## Deferred

Multiclass targets, time-aware validation, probability calibration, fairness assessment requiring
sensitive attributes, causal claims, automated actions, and external model serving.
