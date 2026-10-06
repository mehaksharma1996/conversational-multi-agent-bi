# Brief: #29 Trajectory evaluation, failure recovery, and an advisory judge

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/29
Tier: strongest. Check mode: --full. Policy: [ADR 0008](../../adr/0008-ai-evaluation-policy.md) addendum.

## Outcome

Every evaluation case reports a content-free trajectory and is checked against per-route model-call budgets;
injected provider failures prove documented degradation; an optional, manual LLM-as-judge reports calibration
against human labels and can never affect the CI gate.

## Facts (verified on 2026-10-06)

- `Observation.llm.calls` already records the kind of every model call; `llm_generation_calls` counts only
  generation kinds. Existing maxima (total calls): sql 3, rag 3, hybrid 5, memory 0, unsupported 0.
- `QuestionOrchestrator._classify_route` catches `RuntimeError` (so `LLMGenerationError`) and returns `None` ->
  keyword routing; `_extract_hybrid_criteria` falls back to excerpts.
- `ScriptedLLM.configured` is `"classification" in script`; token usage metadata already exists (#25).

## Do

1. `packages/evaluation/trajectory.py`; call it from `run_case`; `aggregate.trajectory`; evaluator version 1.1.0.
2. `script.failures` in `ScriptedLLM` and fixture validation; `thresholds.json` budgets; `cases/recovery.json`.
3. `packages/evaluation/judge.py`, `scripts/run_judge.py`, `evals/judge/v1/labelled.json` (synthetic).
4. Baseline update; evaluation guide, limitations, ADR 0008 addendum.

## Do not touch

- `scripts/run_evaluations.py` and `runner.py` must not import or mention the judge.
- No private or user data in the labelled set; the judge never runs against scripted fakes or in local-only mode.
- Prompts, retrieval, and safety behavior (this change adds checks only).

## Acceptance

- [ ] Trajectory checks have negative controls (tightened budget, extra/looping calls, removed fallback).
- [ ] Recovery cases pass; injected failures at routing, SQL, answer, and criteria steps are bounded.
- [ ] Judge: opt-in, advisory, calibration + bias probe, provenance recorded, exit 0 regardless of scores.
- [ ] Gate remains offline/deterministic; baseline and docs updated; report schema backward compatible.
- [ ] `python -m scripts.check_all --full` passes.

## Deferred

Real-model calibration and failure scoring (#14); multi-judge ensembles; larger labelled sets.
