# Evaluation methodology and quality gates

The evaluation harness (`packages/evaluation/`, fixtures in `evals/v1/`, CLI
`python -m scripts.run_evaluations`) answers one question: *do the deterministic
safeguards around the model still behave correctly?* It implements
[ADR 0008](../adr/0008-ai-evaluation-policy.md).

## What it does and does not measure

It runs each fixture question through the **real** `QuestionOrchestrator`, SQL guard, SQLite
executor, Chroma store, retriever, grounding checks, and redaction. Only two things are
substituted so the run is offline and repeatable:

- a **scripted model** that returns the output a fixture specifies, including adversarial output
  (destructive SQL, invented citations, instruction-shaped criteria); and
- a **hashing embedder**, a deterministic bag-of-words stand-in for the SentenceTransformer.

So it verifies the system's *reactions* to model behavior, not the model's own accuracy.
There is deliberately **no blended quality score**: every result is a named property check.

Not measured: real-model SQL accuracy, real embedding quality, answer helpfulness, anomaly
precision/recall, or PII recall on real data. The opt-in live mode gives a limited real-model signal
(below).

## What is checked

| Capability | Properties checked (examples) |
|---|---|
| `routing` | Expected route; unavailable-route and low-confidence or malformed classifications ignored; schema-invalid outputs get one bounded repair |
| `text_to_sql` | Read-only validity; required columns/fragments; expected rows and columns; row cap; one bounded correction; refusal of DROP/PRAGMA/ATTACH/catalog reads/disallowed functions; statement tail discarded; dataset intact afterwards |
| `document_rag` | Evidence retrieved from the expected source; irrelevant chunks rejected; citation present and valid; quotes verbatim; uncited and warning statuses reported; **no model call without evidence** |
| `hybrid` | Criteria outputs are schema validated and repaired at most once; criteria keys allowlisted; untraceable values dropped and absent from the SQL prompt; `traced` / `unreferenced` / `excerpt_fallback` provenance; safe fallback to the document answer; approval interrupt, rejection, and unsafe edited approval |
| `memory` | Deterministic answers with zero generation calls; unanswerable memory questions fall through to guarded SQL |
| `unsupported` | No-input and write requests refused without calling the model or altering data |
| `privacy_redaction` | E-mail, phone, and SSN patterns and table sample values never appear in a model prompt |
| `prompt_injection` | Poisoned documents are labeled untrusted; an obeying model still cannot execute unsafe SQL |
| `tenant_isolation` | A second tenant gets 404 on every resource endpoint; owner unaffected; client-supplied tenant headers/payloads ignored; audit streams separated and attributed to the trusted tenant |

The tenant suite (`scripts/api_isolation_eval.py`) drives the FastAPI app because framework-neutral
`packages/` code may not import it.

## Hard failures versus thresholds

Some checks are **critical** (`CRITICAL_CHECKS` in `packages/evaluation/models.py`): unsafe SQL, dataset
modification, invalid citations, grounding failures, prompt redaction failures, untraceable criteria
reaching SQL, tenant-boundary failures, and unexpected crashes. One critical failure fails the gate no
matter how many other checks pass; critical failures can never be written into the baseline.

The remaining checks are grouped by capability with a minimum pass rate in `evals/v1/thresholds.json`. The suite
is deterministic, so every threshold is `1.0`. A threshold below `1.0` is rejected unless it has a
recorded waiver (reason, owner, issue, expiry).

The gate also fails when a check that passed in `evals/v1/baseline.json` now fails, a baseline case
disappears, a new case is not in the baseline, or any of these change without a baseline update:
dataset revision, evaluator version, prompt fingerprints, retrieval settings, or the evaluation embedder.
Prompt fingerprints are digests of the source of the prompt-building functions, so any edit to a prompt
requires review.

## Run locally

```powershell
& ".\.venv\Scripts\python.exe" -m scripts.run_evaluations
& ".\.venv\Scripts\python.exe" -m scripts.run_evaluations --output evals/results/latest.json
& ".\.venv\Scripts\python.exe" -m pytest tests/test_evaluation_harness.py
```

Exit status: `0` gates passed, `1` gates failed, `2` fixture or usage error. `--output` writes a
machine-readable JSON report (`evals/results/` is git-ignored) containing metadata
(dataset version/revision, evaluator version, provider/model, embedding, retrieval settings, prompt
fingerprints), aggregates (per capability, per check, latency), the gate outcome, and per-case checks with
diagnostic counts, including content-free structured-output repair and failure counts. Reports contain no question text, SQL, rows, document text, or invalid structured output. Token usage is `null`
in deterministic mode because no provider is called.

`pytest` also runs the whole suite and the gate, so a regression fails the ordinary test run in CI.

## Add an evaluation case

1. Pick the file in `evals/v1/cases/` for the capability (or add one).
2. Add an object with a unique `id`, `capability`, `question`, `rationale` (why the case exists), `context`
   (`dataset`, `corpus`, and/or `memory`), a `script` of model outputs, and an `expect` block. Use synthetic
   data only; add datasets and corpora to `datasets.json` and `corpora.json` if needed.
3. Assert **properties**, never exact prose. Mark cases that depend on scripted misbehavior with
   `"deterministic_only": true` so live runs skip them.
4. Run the suite. An unknown key in a case is a fixture error, not a skipped check. A model call the case did not script is a
   reported crash, not a silent fallback.
5. Check that the case can fail: temporarily break the behavior it protects and confirm a check fails
   (`tests/test_evaluation_harness.py` does this for the shipped safeguards).
6. Run `--update-baseline` (below) and commit the fixture change and the baseline diff together.

## Interpret a failure

The CLI prints each failing case with its failing checks; `CRITICAL` marks hard failures. Read the report entry for the case: `checks[].detail` explains
which property failed, `diagnostics` shows retrieval counts, grounding status, criteria provenance, and SQL row count.
Then decide whether it is a **product regression** (fix the code), a **fixture error** (fix the fixture and say why in review), or an
**intentional behavior change** (update the expectation and the baseline with a reason). Never loosen a check to make a failure go away without a review comment
explaining what behavior changed.

## Update the baseline without hiding regressions

```powershell
& ".\.venv\Scripts\python.exe" -m scripts.run_evaluations --update-baseline
& ".\.venv\Scripts\python.exe" -m scripts.run_evaluations --update-baseline --accept-regressions "Reason reviewers can read"
```

- Refused if any critical check fails.
- Refused if a previously passing check now fails, unless `--accept-regressions` supplies a written reason; the
  affected cases and the reason are then written into `baseline.json` (`accepted_regressions`) so the acceptance appears in the diff.
- Additions, removals, and improvements are listed on the console and appear in the diff.
- Accepted regressions still count against capability thresholds, so a threshold waiver is also needed.

Reviewers should read the `baseline.json` diff as a behavior change, not a snapshot refresh.

## Review process for prompts, models, embeddings, thresholds, and routing

Any change to a prompt-building function, the model or provider, the embedding model or revision, retrieval settings, a
threshold or waiver, or routing policy must:

1. update or add cases that cover the change;
2. show the before/after of `python -m scripts.run_evaluations` (the gate will already fail on fingerprint or setting drift);
3. record the reason in the pull request and, for regressions or waivers, in `baseline.json` / `thresholds.json`;
4. get review from someone other than the author; and
5. have a rollback path: revert the commit, which restores the previous baseline and fixtures together.

## Optional live evaluation

```powershell
$env:RUN_LIVE_EVALS = "1"   # plus a configured GEMINI_API_KEY in your environment
& ".\.venv\Scripts\python.exe" -m scripts.run_evaluations --live --live-max-cases 8
```

- Off by default and never used in CI. It refuses to start unless `RUN_LIVE_EVALS=1` and Gemini is configured.
- It runs only after the deterministic gates pass, so a live pass can never override a deterministic safety failure.
- Spend is bounded by `--live-max-cases` (default 8); cases marked `deterministic_only` are skipped.
- Output goes to `evals/results/live.json` with `mode: "live"` and the real provider/model, and is never compared to the baseline.
- Live runs send synthetic fixture content to Gemini and are subject to its terms.
- Only the model is real: retrieval still uses the deterministic hashing embedder, so live results say nothing about the SentenceTransformer.
- Live mode fails only on critical checks; other mismatches are reported for a human to read, because real-model wording and choices legitimately vary.
- Token usage is not exposed by the current provider interface, so it is recorded as `null`.
