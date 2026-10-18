# Brief: #136 INV-3 Shared investigation state, guarded tool gateway and process-local checkpoints

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/136
Umbrella: #131. Depends on #133 (INV-0), #134 (INV-1), #135 (INV-2), merged on `main` at `408ab69`.
Tier: strongest (security boundary). Check mode: fast (`python -m scripts.check_all`); no API or
web change.

## Outcome
Investigation state is typed, JSON-native, and changed only through validated single-writer
updates. Restored state is revalidated before it is trusted. The gateway authority, refusal, and
budget vocabulary exists, and checkpoints use a pickle-free serializer that cannot revive
arbitrary objects. Nothing is wired into a route, graph, feature flag, or the API.

Approved scope: stages S0 to S4 below. S5 to S7 (the gateway's executing operations) need separate
approval.

## Facts (verified against the code on 2026-10-17, langgraph 1.2.12, langgraph-checkpoint 4.2.0)
- `JsonPlusSerializer` default msgpack allowlist is permissive. A tampered Pydantic payload is
  revived as the class with invalid values (revival falls back to `model_construct`), and an
  arbitrary-callable payload is executed.
- `JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)` returns Pydantic and
  callable payloads as plain data and never calls them. It still revives `langgraph.types.Interrupt`
  (needed by INV-10). Tuples come back as lists. A DataFrame in state raises `TypeError` at
  checkpoint time; a `"pickle"` blob raises `NotImplementedError`.
- A reducer that raises leaves the rejected update persisted as a pending write. Afterwards
  `get_state()` and resume both raise: the thread is poisoned. A node that raises persists only
  `("__error__", repr(exc))`, so exception text reaches the checkpoint.
- `BinaryOperatorAggregate` channels start at `typ()`: `{}`, `[]`, `0`, `""`.
- `src/orchestration/langgraph_orchestrator.py:build_checkpoint_serializer` and
  `apps/api/approvals.py` use `pickle_fallback=True`; that saver must never hold investigation state.
- `src/orchestration/guarded_sql.py:execute` trusts the caller for prior validation and
  `corrections_used`; `document_research.py` fixes `top_k` at construction, and `SourceRef.filename`
  is the uploaded filename.
- `apps/api/authorization.py`: `Capability.ANALYSIS_RUN == "analysis:run"` lives in `apps/`, which
  `src/` and `packages/` must not import. `apps/api/feature_routes.py` (`create_message`): consent
  holds when `consent_accepted_at` is set and the required recipients are a subset of
  `consent_recipients`.
- `src/llm/fallback.py:_run` tries every usable member inside one logical call. `gemini_client.py`
  retries inside the member and calls `report_usage(..., retries=)` once, only on success; a failed
  member reports nothing. Gemini's `max_output_tokens` defaults to `None`; Ollama sends no output
  cap; Anthropic always sends `MAX_OUTPUT_TOKENS`.
- `src/llm/observability.py:tap_usage` taps see `(prompt_tokens, output_tokens)` only, not retries or
  fallbacks. Its only caller is `packages/evaluation/live_demo.py`.
- `packages/investigation/budget.py`: `settle()` takes no model-call count and `CloseReason` has no
  policy value. `BudgetLedger` holds a lock and a clock and is never checkpointed.

## Contradictions with existing contracts, and their resolution
| # | Contract | Conflict | Resolution |
|---|---|---|---|
| C1 | Ownership table: task status written by the dispatcher | The gateway must be authoritative for attempts | The gateway issues attempt and status updates; the dispatcher is the only node that applies them. Table updated. |
| C2 | `BudgetLedger.settle` records tokens only | Actual provider-call accounting is required | Additive `settle(..., model_calls=)`, with `1 <= model_calls <= reserved`; anything else is a malformed settlement. |
| C3 | `CloseReason` has no policy value | Security refusals must close the ledger | Additive `"policy_refused"`. |
| C4 | ADR 0024 §11: a bound needs enforced caps | A guarantee is required | Model calls are refused unless the whole provider-attempt envelope is bounded (R1). |
| C5 | `TaskOutcome.error_category` must be in `OUTCOME_CATEGORIES` | Refusal codes are finer | Each code maps onto an existing category; the fine code is kept only in the gateway decision. |
| C6 | ADR 0024 §9: "JSON-only" checkpoints | LangGraph stores msgpack | JSON-only means a JSON-native value domain under the strict serializer; tested. |
| C7 | INV-0 contracts use tuples | The serializer returns lists | State stores `model_dump(mode="json")`; restore uses `model_validate`. |
| C8 | INV-1 `SourceRef.filename` | Filenames must stay out of checkpoints | State provenance keeps number, page, chunk index, and score only. |
| C9 | Reducers as the rejection point | Reducer refusals poison the thread | `owned_update` validates inside the node; reducers are defense in depth; unrestorable state fails closed. |
| C10 | Node exceptions are stored as `repr(exc)` | Exception text could carry content | `guarded_node` replaces any exception with a code-only `InvestigationNodeError`, raised `from None`. |

No contradiction is an unresolved blocker for S0 to S4.

## Approved decisions
- **D1** JSON-native state; strict serializer; restored state revalidated before any authorization.
- **D2** State, refusal vocabulary, and authority types live in `packages/investigation/`.
- **D3** Gateway operations will take task IDs, roles, and gateway-issued handles only; prompt text
  comes from the admitted task input. The gateway enforces authorized operations and never plans.
- **D4** Three refusal severities: `request` (recoverable malformed request: nothing runs, nothing is
  reserved, no escalation by count), `task` (that task ends refused or skipped), and
  `investigation` (security, authority, budget, or cancellation: the gateway and ledger close
  permanently for every role). There is no count-based escalation; any future escalation policy
  must be explicit and tested.
- **D5** Authority comes from a trusted adapter and is never read from state or agent output. A live
  authority check that is unavailable refuses. Authority, scope, and approval are rechecked before
  prepared SQL executes.
- **D6/R1** A logical model call reserves the worst case of its whole provider-attempt envelope
  (every usable member, every allowed retry, input and output caps). If any member lacks a known
  attempt bound, an enforced output cap, or a bounded input, the call is refused
  (`metering_unbounded`). Unknown usage, fallbacks, and exceptions are charged the full reservation.
  No cost limit is called guaranteed unless the envelope is bounded. INV-4 may add capped provider
  configurations.
- **D7** SQL that needs approval is refused until INV-10.
- **D8** Rows, passages, SQL text, prompts, and uploaded filenames never enter state or checkpoints.
- **D9/R8** No Supervisor, specialists, Critic, dispatcher, routes, flags, audit actions, or
  telemetry. One gateway serves one sequential investigation: an unexpected concurrent call is
  investigation-terminal (`concurrent_call`). Future parallel attempts (#150) need an explicit,
  separately authorized design.

## Do
S0. This brief; update the ownership table and status in `docs/architecture/investigation-workflow.md`.

S1. `packages/investigation/refusals.py`: `RefusalCode`, `RefusalSeverity`, the severity, outcome
category, and lifecycle-target maps. `packages/investigation/state.py`:
- frozen, `extra="forbid"` models: `InvestigationScope`, `PlanApprovalRecord`, `TaskRecord`,
  `QueryProvenance`, `DocumentProvenance`, `EvidenceRecord`, `GatewayDecision`, `TerminalRecord`;
- `InvestigationState` (`TypedDict`, every channel `Annotated[json type, reducer]`) with the reducer
  rules in the table below;
- `CHANNEL_OWNERS`, `owned_update(owner, current, update)` (check ownership, validate, merge under
  the channel rules, check cross-channel invariants and the status rules below, and return a JSON
  copy of the update as proposed; the reducers store each value canonically as
  `model_dump(mode="json")` when LangGraph applies it) and
  `restore_state(raw) -> InvestigationStateView`;
- `plan_digest(plan)`; code-only `StateUpdateRejected` and `StateInvalid`.

S2. `budget.py`: `settle(model_calls=)` and the `"policy_refused"` close reason;
`refusals.LEDGER_CLOSE_REASON`. `src/llm/observability.py`: `ProviderCallMeter` and
`meter_provider_calls()`. `report_usage` and `report_fallbacks` notify the meter; nothing changes
when no meter is installed. Tests go in a new `tests/test_investigation_metering.py`, so the existing
budget and observability tests stay unmodified.

S3. `packages/investigation/gateway.py`: `ANALYSIS_RUN_CAPABILITY`, `ConsentSnapshot`,
`ProviderAttemptBound` (with a required `data_recipient`), `ProviderEnvelope` with
`logical_call_request()`, `InvestigationAuthority`, `AuthorityStatus`, ports `AuthorityCheck` and
`CancellationToken`, and pure checks `check_authority` and `check_binding`. `authorize_state` runs,
in order: restore (`state_invalid`); binding (scope, approval present, approval and plan digest,
status `running`); capability; consent; approval expiry; the live check last.

S4. `src/orchestration/investigation_checkpoint.py`: `build_investigation_serializer()`,
`new_investigation_saver()`, `is_strict_investigation_saver()`, `guarded_node(owner, fn)`, and
`load_investigation_state()`. Only interrupts pass through `guarded_node`; a returned `Command`
is refused as a non-mapping update and a raised `ParentCommand` becomes a code-only node error.

| Channel | Owner | Rule |
|---|---|---|
| `state_version`, `scope` | intake | write once |
| `status` | lifecycle | first write must be `planning`; then `lifecycle.transition` |
| `plan` | supervisor | write once |
| `admitted_follow_ups` | supervisor | append-only, unique IDs |
| `plan_approval` | approval | write once |
| `tasks` | dispatcher | `pending → running → terminal`, or `pending →` skipped, refused, or cancelled; terminal is immutable; attempts never decrease |
| `evidence` | dispatcher | insert-only; duplicate IDs rejected |
| `decisions` | dispatcher | append-only; `seq` continues strictly |
| `budget_usage` | dispatcher | counters never decrease; close reason written once |
| `terminal` | terminal | write once, matching a terminal `status` |

The gateway is authoritative for attempts, evidence IDs, and budget usage but writes no channel:
the dispatcher applies the updates the gateway issues (C1).

Status rules (`owned_update` only, because they compare against the prior state):
- Execution needs status `running`: a task starting or retrying, new evidence, a decision for a
  call that ran (`succeeded` or `failed`), a new budget reservation, or spending not paired with a
  settling reservation. Otherwise the update is refused with `not_running`.
- After a terminal status (`investigation_terminal`), only bookkeeping is allowed: closing
  existing tasks (attempts unchanged), recording refused or cancelled decisions, settling
  reservations made while running (spending rises only while outstanding reservations fall), the
  close reason, elapsed time, and the terminal record. The plan, follow-ups, and plan approval are
  frozen, and no new task record may be created.
- Restore also rejects a `planning`, `awaiting_plan_approval`, or `queued` investigation that has
  any started or ended task or any evidence (`not_running`).
- `state_version` must be an exact `int` (no `bool` or `float`) both when written and when restored.

## Review corrections (F1 to F7, approved 2026-10-17)
- **F1** status rules above. **F3** exact-`int` state version. **F4** legacy `"json"` constructor
  blobs (`lc` 1 or 2) are tested: no trusted class is revived and no code is called.
- **F2** each `ProviderAttemptBound` states its `data_recipient` (required, `None` only for a
  provider that keeps data local). An `InvestigationAuthority` whose `required_recipients` omit a
  member's recipient is rejected at construction, and consent is checked against the union of
  both, so the envelope binds consent even if validation was bypassed. Agents never supply either.
- **F5** a meter that raises never breaks the provider call or ordinary reporting; the
  `MeterHealth` yielded by `meter_provider_calls` is marked `faulted` and the exception is dropped.
  **S5 obligation:** when the meter faulted, the metered block raised, or a member failed without
  reporting, settle the reservation without counts (the full reservation), never the partial
  counts and never zero. (S5 H2/H3: settled with `usage_complete=False`, which charges the larger of
  the reservation and the reported totals; a meter fault is also `metering_violation`.)
- **F6** this brief and the module docstrings now match ownership, reducer canonicalization,
  Command handling, and the authorization-check order.
- **F7** `load_investigation_state` refuses (`checkpointer_not_strict`) unless the graph's
  checkpointer is exactly an `InMemorySaver` with the strict serializer (no pickle, no extra msgpack
  or JSON allowlist, no custom hook). The check runs before any blob is read.

## Later stages (not approved yet)
S5 gateway core (pre-flight order, attempt tokens, `AdmittedLLMClient`, metering reconciliation,
document research). S6 single-use SQL handles (prepare, execute, one correction). S7 bypass tests and
an ADR 0024 amendment for C4, C6, and C9.

## Do not touch
`src/orchestration/{langgraph_orchestrator,graph_state,guarded_sql,document_research,
policy_grounding}.py`, `src/agents/*`, `src/storage/*`, `apps/**`, `evals/**`, the OpenAPI schema,
`packages/governance/audit.py`, telemetry allowlists, dependency and lock files, and Docker files.
The existing question graph, its checkpoint saver, the API, and approvals keep their behavior.

## Acceptance
- [ ] Reducers and `owned_update` reject evidence overwrites, illegal task and lifecycle
      transitions, rewritten write-once channels, decreasing budgets, and foreign-owner writes.
      A rejected multi-channel update applies nothing.
- [ ] State round-trips through the checkpointer without pickle; every stored channel value is
      JSON-native.
- [ ] Tampered, arbitrary-object, and unvalidated Pydantic payloads never become trusted state; a
      poisoned thread fails closed in `load_investigation_state`.
- [ ] Node exceptions reach the checkpoint only as code-only messages (canary test).
- [ ] Tasks cannot start, retry, or produce evidence unless the investigation is `running`; after a
      terminal status only the bookkeeping above is accepted (cancelled and completed tested).
- [ ] An authority whose consent requirement misses a hosted envelope member is rejected.
- [ ] A meter fault is recorded, never raised into the provider path, and never reads as zero.
- [ ] Loading refuses any checkpointer other than the strict investigation saver.
- [ ] `check_authority` refuses a missing capability, missing or mismatched consent, a revoked or
      unavailable live check, and an expired or mismatched approval. Agent-supplied state is never
      an input to the authority decision.
- [ ] `ProviderEnvelope` refuses unbounded members and reserves the worst case across all members
      and attempts. `settle(model_calls=)` rejects 0 and booleans; a value above the reservation (S5 H3) is charged as
      metered, closes the ledger (`reservation_exceeded`), and raises `ReservationExceededError`.
- [ ] The meter changes nothing when it is not installed (existing observability tests unchanged).
- [ ] `test_orchestrator_characterization.py`, `test_langgraph_orchestrator.py`, and
      `test_guarded_services.py` pass unmodified, and `python -m scripts.check_all` passes.

## Evaluation and docs impact
No `evals/` or baseline change; no prompt, retrieval, or safety-behavior change. Update the
investigation-workflow doc. The ADR 0024 amendment waits for S7.

## Design decisions already made
Pydantic v2 models, frozen, `extra="forbid"`, not model-wide strict (as INV-0). Error messages
carry codes only. Plan digest is SHA-256 of canonical JSON (`sort_keys`, compact separators).
Evidence IDs are `ev_` plus 32 lowercase hex characters.
