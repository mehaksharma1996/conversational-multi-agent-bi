# Evaluation assets

Sanitized, synthetic, versioned inputs for evaluating routing, guarded text-to-SQL, document
RAG, hybrid provenance, memory, unsupported requests, PII redaction, and prompt injection.
Methodology, gates, and the review process are in
[docs/governance/evaluation.md](../docs/governance/evaluation.md).

## Layout

- `v1/` is the current fixture set:
  - `manifest.json`: dataset version and revision, retrieval settings.
  - `datasets.json`, `corpora.json`: invented tables and documents.
  - `cases/*.json`: one file per capability.
  - `thresholds.json`: per-capability minimum pass rates (waivers required below 1.0).
  - `baseline.json`: committed results the gate compares against.
- `baseline/cases.json` is the earlier characterization record of routing behavior at the start of the
  modernization. It is kept for history and is still checked by `tests/test_evaluation_baseline.py`.
- `results/` (git-ignored) receives generated JSON reports.

## Rules

- Every case has a stable unique ID, capability, rationale, and property-based expectation.
  Never assert exact model prose.
- Fixtures must be synthetic or public. Never copy real user data into a fixture.
- Safety and routing gates run without credentials or network access.
- Live-provider results are recorded separately (`--live`) and never override deterministic gates.
- Updating an expectation or the baseline is a behavior change that needs a written reason in review,
  not a snapshot refresh.

## Common commands

```powershell
& ".\.venv\Scripts\python.exe" -m scripts.run_evaluations
& ".\.venv\Scripts\python.exe" -m scripts.run_evaluations --output evals/results/latest.json
& ".\.venv\Scripts\python.exe" -m scripts.run_evaluations --update-baseline
```
