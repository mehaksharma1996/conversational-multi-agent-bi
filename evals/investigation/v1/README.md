# Subscription churn investigation fixture v1

This fixture supports comparisons between the existing SQL, document, and hybrid question routes
and the future multi-agent investigation workflow described by ADR 0024.

## Boundary

- Model-accessible source material: `sample_data/subscription_churn.csv` and the four
  `sample_data/subscription_churn_*.pdf` files only.
- Public evaluation input: `public_cases.json`, which contains questions but no expected values,
  verdicts, reference queries, or forbidden-claim answers.
- Evaluator-only input: `oracle.json` and `reference.sql`. Evaluation-specific code may read these
  only after a run returns; application, retrieval, and prompt-building code must not read them.

The oracle is committed and therefore publicly inspectable on GitHub. It is not a secret or hidden
benchmark. Its isolation prevents the evaluated system from receiving its own expected answers.

`manifest.json` uses the existing workflow manifest schema, `sha256-lf`/`sha256-raw` conventions,
and coverage vocabulary. It is deliberately separate from `evals/baseline/v2`: INV-2 does not
retrofit unimplemented investigation behavior into the existing scripted baseline.

Regenerate or check the assets from the repository root:

```powershell
python -m scripts.generate_subscription_churn
python -m scripts.generate_subscription_churn --check
```
