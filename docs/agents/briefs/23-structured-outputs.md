# Brief: #23 Schema-validated structured outputs

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/23
Tier: medium. Check mode: fast, plus `--only evaluations` after the baseline update.

## Outcome
Route classification and hybrid criteria extraction return Pydantic-validated results. A malformed
model answer gets at most one repair retry (which sends only an error category, never content) and
the attempt count is recorded in content-free telemetry. The existing deterministic defenses still
run after validation.

## Facts (verified 2026-10-05)
- `src/orchestration/langgraph_orchestrator.py`:
  - `_classify_route` (about line 186) builds a prompt, calls `self.llm_client.generate(prompt)`,
    runs `_parse_json_object(response.text)`, and accepts the route only if it is in the available
    routes and `confidence >= 0.55`; any failure returns `None` (keyword routing then decides).
  - `_extract_hybrid_criteria` (about line 384) does the same, then `_sanitize_hybrid_criteria`
    (key allowlist `_ALLOWED_HYBRID_CRITERIA_KEYS`, `_MAX_HYBRID_CRITERIA_CHARS = 2000`, and
    trace-to-excerpt check on strings). On error it returns provenance `excerpt_fallback`.
  - `_parse_json_object` (about line 523) strips a fence and `json.loads` the first `{` to last `}`.
- `src/llm/base.py`: `LLMClient` Protocol with `provider`, `model`, `generate(prompt) -> LLMResponse`.
  `LLMResponse(text, model, provider)`. Errors: `LLMConfigurationError`, `LLMGenerationError`.
- `src/llm/gemini_client.py`: `GeminiClient.generate` builds `kwargs["config"]` with temperature 0
  and a system instruction; this is where Gemini `response_mime_type` / `response_schema` go.
- `packages/evaluation/fakes.py`: `ScriptedLLM`, `RecordingLLM`, `CountingLLM` implement `generate`
  only. They must keep working through the text-only fallback.
- `src/orchestration/graph_state.py`: `AnswerDiagnostics` holds content-free counts and enums that
  telemetry, the API provenance summary, and the eval harness all read.
- `packages/observability/telemetry.py`: `ALLOWED_ATTRIBUTES: dict[str, str]` (name to declared
  kind). Add only int counters.
- `packages/evaluation/runner.py:prompt_fingerprints` hashes the prompt-building functions, so the
  baseline in `evals/v1/baseline.json` must be regenerated per `docs/governance/evaluation.md`.
- Eval cases: `evals/v1/cases/routing.json`, `evals/v1/cases/hybrid.json`.

## Do
1. Add Pydantic models (new module, for example `src/llm/structured.py`): `RouteDecision`
   (`route` limited to the available routes, `confidence` 0..1) and a hybrid criteria envelope
   with the six allowlisted keys, `extra="forbid"`.
2. Extend `LLMClient` with an optional structured capability (a separate `Protocol` such as
   `StructuredLLMClient.generate_structured(prompt, schema_model)`); detect it with `isinstance`
   or `getattr` and keep the text-only path for clients without it (including the eval fakes).
3. Implement it in `GeminiClient` with `response_mime_type="application/json"` and a response
   schema, then validate with the model. Text-only path: parse then validate with the same model.
4. Add one bounded repair retry that sends only the validation error category. Never send or log
   the invalid content. Track `repairs` and `failures` counts.
5. Replace both `_parse_json_object` call sites. Keep `_sanitize_hybrid_criteria` and the
   trace-to-excerpt check unchanged and still applied after validation. Remove
   `_parse_json_object`, or keep it as a documented compatibility fallback used only by the
   text-only path.
6. Surface the two counters through `AnswerDiagnostics` and add `structured_output_repairs` and
   `structured_output_failures` (int) to `ALLOWED_ATTRIBUTES`. The telemetry attributes are built
   in `apps/api/feature_routes.py` (about line 580, the dict containing
   `"sql_correction_attempted"`); add the two counters there, and to the eval harness reader in
   `packages/evaluation/evaluators.py` if cases assert on them.
7. Tests: repair loop never exceeds one retry; text-only fallback works; malformed, extra-key,
   wrong-type, out-of-range outputs are rejected; telemetry contains no content.
8. Add eval cases to `routing.json` and `hybrid.json` for malformed, extra-key, wrong-type and
   out-of-range outputs, including a repaired response that still goes through criteria tracing.
9. Regenerate the baseline as documented in `docs/governance/evaluation.md`; update that doc and
   the responsible-AI notes if behavior changes.

## Do not touch
- The SQL guard (`src/storage/query_executor.py`) and `src/agents/sql_agent.py`.
- `_sanitize_hybrid_criteria` logic, the 0.55 confidence threshold, and keyword fallback routing.
- `packages/` must not import `apps/`, FastAPI, or Streamlit (`tests/test_package_boundaries.py`).
- No prompt, response, or invalid payload text in logs, telemetry, audit, or error messages.

## Acceptance
- [ ] Route classification and criteria extraction use validated models.
- [ ] Eval cases for malformed, extra-key, wrong-type, out-of-range, and repaired outputs.
- [ ] Baseline updated per ADR 0008; mention the prompt-fingerprint change in the commit body.
- [ ] Unit tests for the one-retry bound and the unsupported-structured fallback.
- [ ] `python -m scripts.check_all` passes.

## Evaluation and docs impact
Prompt fingerprints change, so `evals/v1/baseline.json` is regenerated. Update
`docs/governance/evaluation.md`. No ADR is needed unless a trust boundary changes.

## Design decisions already made
- Pydantic v2 (`pydantic==2.13.5` is already in `requirements.lock` via FastAPI). Because `src/`
  will now import it directly, also add a `pydantic` pin to `requirements.txt` at the locked
  version. `extra="forbid"` on both models.
- Maximum one repair retry. Repair prompt carries an error category only.
- Provider capability is detected, never request-selectable.
