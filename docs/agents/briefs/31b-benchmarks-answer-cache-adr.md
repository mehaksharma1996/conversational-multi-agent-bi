# Brief: #31b Benchmarks and answer-cache privacy decision

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/31
Tier: medium. Check mode: --full.

## Outcome

The deterministic core has a repeatable, dependency-free benchmark with committed indicative baselines
and a non-blocking CI report, and the answer-cache privacy decision is recorded (do not implement yet)
before any such cache exists. This slice completes #31.

## Facts (verified against the code on 2026-10-06)

- `scripts/run_benchmarks.py`: seeded synthetic data; groups `analysis`, `sql`, `retrieval`, `pdf`;
  `--quick`, `--only`, `--output`, `--profile`; never fails on timing.
- `packages/evaluation/fakes.py:HashingEmbedder` is the offline embedder; no model download.
- `docs/operations/benchmarks.md` holds the baseline, cProfile findings, and the inputs for #10.
- `docs/adr/0019-answer-cache-privacy.md` records the answer-cache decision and the conditions for any
  future implementation.

## Do not touch

- Streaming, pagination, idempotency, jobs, workers, or cancellation (#10/#17). Findings are fed to #10
  in prose; no job design here.
- `profile_dataframe` behavior: the date-detection sampling opportunity is documented, not changed.
- Any answer/LLM-response cache implementation, and any hard performance threshold in CI.
- Telemetry/audit allowlists; benchmark reports contain sizes and timings only.

## Acceptance

- [x] Benchmarks for analysis pipeline, SQL validation/execution, retrieval, and PDF indexing at
      representative synthetic sizes; run locally and in a non-blocking CI job.
- [x] Environment info and measured baselines in `docs/operations/benchmarks.md`; surprising results
      profiled with cProfile; relevant findings listed for #10.
- [x] ADR 0019 records the privacy decision (no answer cache yet), the provider-side caching alternative,
      and the requirements if one is implemented anyway.
- [x] A smoke test proves the runner works, is deterministic, and emits no content.

## Evaluation and docs impact

`evals/v1/` and its baseline are unchanged (no prompt, retrieval, fixture, or safety change). Docs:
`docs/operations/benchmarks.md`, ADR 0019 and the ADR index, AGENTS.md command list.

## Design decisions already made

- Standard library timing (`perf_counter`, `statistics`, `cProfile`); no `pytest-benchmark` dependency.
- Timings are indicative and are never asserted in tests or enforced in CI.
- The answer cache is deferred; provider-side implicit prefix caching needs no application state.
