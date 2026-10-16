# Brief: #135 INV-2 Add the synthetic churn investigation fixture and oracles

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/135
Tier: medium. Check mode: fast.

## Outcome
A seeded generator produces one customer-quarter churn CSV, four synthetic business PDFs, a
separate investigation-v1 manifest, public cases, evaluator-only ground truth, and reference SQL.
The fixture exercises current SQL, document, and hybrid capabilities and supplies stable evidence
for the future five-role investigation workflow without changing runtime behaviour.

## Facts (verified against the code on 2026-10-16)
- `scripts/generate_retail_demo.py` uses fixed seeds, decimal arithmetic, LF-delimited text, and
  ReportLab invariant mode; `tests/test_retail_demo.py` compares generated assets byte-for-byte.
- `packages/evaluation/workflow_cases.py` provides the existing manifest schema, coverage labels,
  hash conventions, and validator.
- `evals/baseline/v2/workflow-baseline.json` pins the exact v2 manifest and case set, including
  unmeasured cases, so INV-2 must not retrofit churn cases into that baseline.
- `src/ingestion/tabular_loader.py` accepts one CSV/XLS/XLSX table; the question workflow binds one
  `StoredTable`. A denormalized customer-quarter CSV matches the supported product boundary.
- `packages/retrieval/service.py` indexes PDF pages with filename, page, chunk, and document-hash
  provenance. Oracle files must never enter that document list.
- `src/storage/query_executor.py` permits one bounded read-only SELECT/CTE with table, column, and
  function allowlists, a 500-row cap, and a five-second default deadline.

## Do
1. Add `scripts/generate_subscription_churn.py` with seed `20261013`; generate approximately 8,200
   customer-quarter records for 2025-Q3 through 2026-Q2 and four invariant PDFs.
2. Derive the evaluator-only oracle independently in Python and validate it with named SQLite
   reference queries. Plant 4.8% to 7.1% overall churn, SMB Basic 6% to 14%, APAC 5% to 9%, flat
   Enterprise churn, the tenure confound, exact MRR loss, and 40% missing reason codes.
3. Add eight answer-free public scenarios plus an isolated `evals/investigation/v1/manifest.json`
   using the existing hash and coverage conventions. Label future multi-step cases
   `requires_agent`; do not attach them to the v2 scripted baseline.
4. Add fixture, arithmetic, lifecycle, document extraction, retrieval provenance, prompt-boundary,
   manifest, and oracle-isolation tests.
5. Update only `sample_data/README.md` and `evals/README.md`.

## Do not touch
`evals/v1/`, `evals/baseline/v2/workflow-baseline.json`, existing thresholds, prompt fingerprints,
LangGraph topology, runtime services, SQL authorization, approvals, API, web client, dependencies,
or existing worktrees. Do not implement investigation agents, a gateway, API routes, or UI.

## Acceptance
- [ ] Generation is byte-reproducible with seed `20261013` and all artifacts have pinned hashes.
- [ ] Reference SQL reproduces 4.8% to 7.1% overall, 6% to 14% SMB Basic, 5% to 9% APAC, flat
      Enterprise churn, the tenure adjustment, MRR totals, and 40% missing reason codes.
- [ ] H1 and H2 are `supported_association`, H3 is `untestable`, and H4 is `refuted`.
- [ ] Only the CSV and four PDFs are model-accessible; public cases expose no oracle answers,
      verdicts, reference SQL, or forbidden-claim answers.
- [ ] The malicious memo sentence remains untrusted document content; tests make no live-model
      resistance claim.
- [ ] Existing deterministic evaluations and prompt fingerprints are unchanged.
- [ ] `python -m scripts.check_all` passes.

## Evaluation and docs impact
Create `evals/investigation/v1/` as a fixture-only, schema-compatible manifest. Do not update any
existing golden baseline. Document fixture use and the fact that a committed oracle is public on
GitHub, evaluator-only rather than secret. No ADR is needed.

## Design decisions already made
One denormalized CSV; four quarters; approximately 8,200 rows; seed `20261013`; four PDFs; eight
scenarios; a separate investigation-v1 manifest; all pricing changes apply to active SMB Basic
accounts on the documented effective date, so the hybrid case uses observable plan and quarter
fields rather than a derived exposure label. Keep changes uncommitted for review.
