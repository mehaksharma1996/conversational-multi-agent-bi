# Routing contract

The orchestrator chooses one route per question. When a model is configured it classifies the
question first; the deterministic router in `route_question` is the fallback when the model is
unavailable, invalid, low-confidence (below 0.55), or proposes a route whose inputs do not exist.

| Route | Meaning |
|---|---|
| `sql` | Answerable entirely from the uploaded table; any filter or threshold is in the question or is an existing column |
| `rag` | Answerable from uploaded documents alone, without counting or calculating over rows |
| `hybrid` | A rule, threshold, definition, or criterion stated in the documents must be applied to select, count, aggregate, or interpret table rows |
| `memory` | About prior analysis, charts, reports, or generated SQL |
| `unsupported` | Neither the table nor the documents can supply the answer |

Route prompt v2 (registry id `route-classification`) states these definitions, decision rules
(a similar-sounding column does not define a documented rule; mentioning "policy" alone does not
make a question hybrid), general-pattern examples, and the last two prior questions as context
only. It does not include table contents, column names, or document text.

## Consistency check

Two inconsistencies cost one extra model call each, at most once per question:

- the model chose `sql`, documents exist, and the question names a document-defined criterion
  (policy, rule, threshold, escalation, eligibility, breach, and similar cues); or
- the model chose `rag`, a table exists, and the question counts or aggregates rows.

The cue only prompts a reconsideration with a fixed consistency note. The model's second answer is
used only if it is an available route at or above the confidence floor; otherwise the first route
stands. The check never forces a route, and it is inactive when `hybrid` is unavailable (no table or
no documents). A cue-free question is never reconsidered.

## What route choice cannot bypass

Choosing `hybrid` only selects the workflow. Generated SQL still passes read-only validation, the
policy-to-SQL predicate-completeness check, optional human approval, the guarded executor, and the documented
fallback to document-only answers. The route does not affect tenant or workspace authorization,
which is enforced before the orchestrator runs.

Answer diagnostics record the initial model route and confidence, whether reconsideration ran, the
final route and confidence when supplied by the model, whether deterministic routing was used, and
the policy-to-SQL outcome. These are bounded enums, numbers, and booleans; no reasoning text or
prompt content is recorded. If a later safety check changes the delivered route (for example, a
hybrid predicate mismatch falls back to documents), the final route reflects the delivered result
and its model-confidence field is cleared rather than attaching the hybrid decision's confidence to
the document-only fallback.

## Known limits

- The cue list is lexical and English-only. A hybrid question phrased without any cue is caught
  only by the model's first answer.
- Diagnostics show that reconsideration happened, but never expose hidden reasoning or the
  consistency prompt.
- Real-model behavior is measured only by the opt-in live evaluation, not by `evals/v1`.
