# Brief: #133 INV-0 Record the investigation ADR and minimal typed contracts

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/133
Tier: medium. Check mode: fast.

## Outcome
ADR 0024 records the approved multi-agent investigation architecture and decisions. A
framework-neutral `packages/investigation/` package holds minimal versioned contracts, deterministic
plan and follow-up validation, a fail-closed budget ledger, an honest cost estimate, and the
lifecycle state machine. Nothing is wired into runtime, the API, or the UI.

## Facts (verified against the code on 2026-10-15)
- `tests/test_package_boundaries.py`: `packages/` must not import `apps`, `fastapi`, `src.ui`, or
  `streamlit`. This package is stricter: it imports no LangGraph, `src`, or `apps`.
- `packages/jobs/contract.py`: a `TRANSITIONS` table and terminal set. The lifecycle mirrors this style.
- `src/llm/fallback.py:72`: the fallback provider validates parsed JSON with `model_validate(dict)`.
  Strict Pydantic mode would reject lists for tuples there, so the contracts forbid extra fields but
  are not `strict`.
- `src/orchestration/langgraph_orchestrator.py:140-149`: the question-graph serializer uses
  `pickle_fallback=True`. The investigation contracts must round-trip through plain JSON.
- `src/llm/observability.py:LLMPricing`: pricing is optional operator configuration, and Gemini's
  `max_output_tokens` is optional. An enforceable cost ceiling cannot be assumed.
- `packages/observability/errors.py:ERROR_CATEGORIES`: the shared safe-category vocabulary that
  task outcomes extend.

## Do
1. ADR 0024 plus an entry in `docs/adr/README.md`; `docs/architecture/investigation-workflow.md`.
2. `packages/investigation/contracts.py`, `validation.py`, `budget.py`, `lifecycle.py`, and `__init__.py`.
3. Tests: `tests/test_investigation_{contracts,plan_validation,budget,lifecycle}.py`, the shared
   builders in `tests/investigation_fixtures.py`, and a framework-neutral import check in
   `tests/test_package_boundaries.py`.
4. One `packages/investigation/` line in the AGENTS.md architecture list.

## Do not touch
`src/`, `apps/`, `evals/`, the OpenAPI schema, telemetry and audit allowlists, dependency files, and
the web client. No prompts, model calls, LangGraph nodes, or persistence. No evidence payload,
hypothesis, critique, or report contracts: INV-5 to INV-8 add them where they are used.

## Acceptance
- [ ] `python -m scripts.check_all` passes; evaluations and OpenAPI are unchanged.
- [ ] Every validator rule has a positive and a negative test; all models round-trip through JSON
      (both `model_validate_json` and `model_validate` of parsed JSON) without pickle.
- [ ] `CostEstimate.bound` is `enforced_upper_bound` only when it is enforceable for all eligible
      providers, fallbacks included; otherwise it is `indicative` or `unavailable`.
- [ ] Ledger: rejects invalid charges; enforces limits before work, counting outstanding
      reservations; accumulates retries and follow-ups; uses a monotonic clock; never resets; charges
      the full reservation for unreported usage; closes permanently on exhaustion or fault.
- [ ] The five-role conformance test and the boundary tests pass.

## Evaluation and docs impact
No `evals/v1` or baseline change. New ADR 0024 and architecture doc; one line in AGENTS.md.

## Design decisions already made
Pydantic v2, frozen, `extra="forbid"`; framework-neutral; sequential execution; budget numbers as in
ADR 0024; no causal hypothesis status (INV-6); cost figures are always labelled as estimates.
