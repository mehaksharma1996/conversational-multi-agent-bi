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
- `baseline/v2/` is the versioned workflow case set (Iteration 2, E1b):
  - `manifest.json`: every live-demo case and investigation case, each with one category and one
    coverage label (`measured`, `not_covered_by_script`, `requires_agent`, `requires_live_model`,
    `unsupported`), plus pinned hashes of the retail CSV, policy PDF, ground truth, and both case
    files. Validated by `packages/evaluation/workflow_cases.py` and `tests/test_workflow_cases.py`.
  - `investigation_cases.json`: three multi-step investigation questions with oracle reference
    queries checked against the committed CSV and ground truth. None is scored offline.
  - Changing a pinned file, a case id, or a coverage label is a reviewed change: update the
    manifest hash or entry in the same pull request and say why.
  - `workflow-baseline.json`: the deterministic scripted-run baseline (E1b-2): per-case outcomes,
    coverage counts, Wilson 95% intervals, and five safety invariants. It is application-path
    regression evidence, not model accuracy. `tests/test_workflow_baseline.py` re-runs all cases
    and requires an exact match; updates go through
    `python -m tests.e2e.workflow_baseline_runner --update --justification "..."`, which refuses
    safety failures and any regression, deletion, or demotion of a previously measured case.
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
