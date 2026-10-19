"""INV-4 (#137) S2d: one repair at most, one cumulative ledger, and the terminal state machine.

The gateway has no public seam for its trusted inputs changing between the two calls, by design. The
adversarial tests that simulate such a change, or another holder of the shared ledger, do so only
through ``_tamper`` and ``_occupy_ledger`` below, which are the only places this module touches
private gateway state; the meter-fault test also patches the private ``_CallMeter``. No production
API exists to support them.
"""

from __future__ import annotations

import csv
import json
import socket
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from packages.investigation import PLANNING_LIMITS
from packages.investigation.budget import BudgetRequest
from packages.investigation.contracts import InvestigationPlan
from packages.investigation.gateway import (
    PLANNING_STAGE_INPUT_TOKENS,
    PLANNING_STAGE_OUTPUT_TOKENS,
    AuthorityStatus,
    ConsentSnapshot,
    PlanningOutcomeCode,
    PlanningStage,
    ProviderEnvelope,
)
from packages.investigation.planning import (
    DocumentTitleMetadata,
    PlanningContext,
    PlanningMetadata,
    PlanningValidationError,
    SchemaFieldMetadata,
)
from packages.investigation.refusals import RefusalCode
from packages.investigation.state import plan_digest
from src.llm.base import LLMResponse
from src.llm.bounded import DeadlineModelResult, DeadlineTermination
from src.orchestration import investigation_planning
from src.orchestration.investigation_planning import (
    InvestigationPlanningGateway,
    PlanningCallResult,
    PlanningCallStatus,
)
from src.orchestration.planning_prompt import render_initial_prompt, render_repair_prompt
from tests.investigation_fixtures import churn_plan, document_task, plan_of, sql_task
from tests.investigation_planning_fakes import (
    BOUND,
    CANARY,
    Call,
    Cancellation,
    FakeClock,
    LiveAuthority,
    ScriptedDeadlineAdapter,
    authority,
    completed,
    context,
    evidence,
    metadata,
    pricing,
    report_fallbacks,
    report_served_by,
    report_usage,
    scope,
    stopped,
    valid_response_text,
)

ROOT = Path(__file__).resolve().parents[1]
C = RefusalCode
P = PlanningOutcomeCode
SECRET_SQL = "SELECT card_number FROM payments"
INVALID_TEXT = json.dumps(
    {
        "plan": {**churn_plan().model_dump(mode="json"), "tasks": []},
        "note": f"{CANARY} {SECRET_SQL}",
    }
)
UNKNOWN_FIELD_TEXT = json.dumps(
    {
        "plan": plan_of(
            sql_task("trend", fields=("quarter", "churned")),
            sql_task("segments", depends_on=("trend",), fields=("quarter", "no_such_field")),
        ).model_dump(mode="json"),
        "missing_information": [],
    }
)


class Harness:
    def __init__(
        self,
        *,
        adapter: Any = None,
        context_value: PlanningContext | None = None,
        authority_value: Any = None,
        evidence_value: Any = None,
        live: LiveAuthority | None = None,
        clock: FakeClock | None = None,
        cancellation: Cancellation | None = None,
    ) -> None:
        self.adapter = adapter if adapter is not None else ScriptedDeadlineAdapter()
        self.clock = clock or FakeClock()
        self.cancellation = cancellation or Cancellation()
        self.live = live or LiveAuthority()
        self.gateway = InvestigationPlanningGateway(
            authority=authority_value if authority_value is not None else authority(),
            context=context_value if context_value is not None else context(),
            adapter=self.adapter,
            evidence=evidence_value if evidence_value is not None else evidence(),
            pricing=pricing(),
            live=self.live,
            cancellation=self.cancellation,
            clock=self.clock,
        )

    @property
    def model_calls(self) -> int:
        return len(self.adapter.calls)

    @property
    def counts(self) -> int:
        return len(self.adapter.counted_prompts)


def _tamper(gateway: InvestigationPlanningGateway, **fields: Any) -> None:
    """Adversarial only: overwrite private gateway state to simulate a change between calls."""
    for name, value in fields.items():
        assert name.startswith("_") and hasattr(gateway, name), name
        setattr(gateway, name, value)


def _occupy_ledger(gateway: InvestigationPlanningGateway, request: BudgetRequest) -> None:
    """Adversarial only: another holder of the same ledger reserves part of the allowance."""
    gateway._ledger.authorize(request)


def _answers(*texts: str, **kwargs: Any) -> ScriptedDeadlineAdapter:
    return ScriptedDeadlineAdapter(answers=list(texts), **kwargs)


def _repairable(**kwargs: Any) -> Harness:
    """A gateway whose initial candidate was invalid, so exactly one repair is available."""
    h = Harness(**kwargs)
    first = h.gateway.initial()
    assert first.status is PlanningCallStatus.INVALID
    return h


def _per_call(*scripts: Callable[[ScriptedDeadlineAdapter, Call], DeadlineModelResult]) -> Any:
    def script(adapter: ScriptedDeadlineAdapter, call: Call) -> DeadlineModelResult:
        return scripts[len(adapter.calls) - 1](adapter, call)

    return script


def _answer_with(
    text: str,
    *,
    reports: tuple[dict[str, Any], ...] = ({"prompt_tokens": 1_234, "output_tokens": 321},),
    served: tuple[tuple[str, str], ...] = (("scripted", "planner-v1"),),
    fallbacks: int = 0,
    identity: tuple[str, str] | None = None,
) -> Callable[[ScriptedDeadlineAdapter, Call], DeadlineModelResult]:
    def script(adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        for report in reports:
            report_usage(**report)
        for provider, model in served:
            report_served_by(provider, model)
        if fallbacks:
            report_fallbacks(fallbacks)
        if identity is None:
            return completed(adapter, text)
        return DeadlineModelResult(
            response=LLMResponse(text=text, provider=identity[0], model=identity[1]),
            termination=DeadlineTermination.COMPLETED,
            cancellation_requested=False,
            termination_confirmed=True,
            provider_execution_bound_enforced=True,
        )

    return script


INVALID_FIRST = _answer_with(INVALID_TEXT)


@pytest.fixture(autouse=True)
def no_tools_or_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spies: any SQL, retrieval, document service, socket, or tool gateway use fails the test."""
    touched: list[str] = []

    def spy(name: str) -> Any:
        def refuse(*_args: object, **_kwargs: object) -> Any:
            touched.append(name)
            raise AssertionError(f"planning reached {name}")

        return refuse

    from src.documents import retriever
    from src.orchestration import document_research, guarded_sql, tool_gateway

    monkeypatch.setattr(sqlite3, "connect", spy("sqlite3.connect"))
    monkeypatch.setattr(socket, "socket", spy("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", spy("socket.create_connection"))
    monkeypatch.setattr(guarded_sql.GuardedSQLService, "__init__", spy("GuardedSQLService"))
    monkeypatch.setattr(
        document_research.DocumentResearchService, "__init__", spy("DocumentResearchService")
    )
    monkeypatch.setattr(retriever.Retriever, "__init__", spy("Retriever"))
    monkeypatch.setattr(
        tool_gateway.InvestigationToolGateway, "__init__", spy("InvestigationToolGateway")
    )
    return touched


def _refused(result: PlanningCallResult, code: object) -> None:
    assert result.status is PlanningCallStatus.REFUSED, result
    assert result.code == code
    assert result.proposal is None and result.errors == ()


def _usage_counts(h: Harness) -> tuple[int, ...]:
    usage = h.gateway.usage()
    return (
        usage.model_calls,
        usage.input_tokens,
        usage.output_tokens,
        usage.released_reservations,
        usage.outstanding_reservations,
        usage.verified_model_settlements,
        usage.unmetered_model_calls,
    )


# --- the three successful sequences ----------------------------------------------------------


def test_invalid_initial_then_valid_repair_is_proposed_within_one_ledger() -> None:
    h = Harness(adapter=_answers(INVALID_TEXT, valid_response_text()))
    first = h.gateway.initial()
    assert first.status is PlanningCallStatus.INVALID and first.stage is PlanningStage.INITIAL
    assert first.usage.closed_reason is None

    second = h.gateway.repair()
    assert second.status is PlanningCallStatus.PROPOSED and second.stage is PlanningStage.REPAIR
    assert second.proposal is not None and second.proposal.plan == churn_plan()
    assert plan_digest(second.proposal.plan) == plan_digest(churn_plan())
    assert second.cost_estimate == first.cost_estimate

    usage = second.usage
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (2, 2_468, 642)
    assert usage.verified_model_settlements == 2 and usage.reservation_bound == "held"
    assert usage.closed_reason == "completed" and usage.outstanding_reservations == 0
    assert h.model_calls == 2 and h.counts == 2
    assert len(h.live.calls) == 2  # the live authority is re-checked before the repair
    for call in h.adapter.calls:
        assert call.max_output_tokens == PLANNING_STAGE_OUTPUT_TOKENS


def test_the_repair_prompt_is_the_fixed_renderer_over_the_same_metadata_and_codes() -> None:
    h = Harness(adapter=_answers(INVALID_TEXT, valid_response_text()))
    first = h.gateway.initial()
    h.gateway.repair()
    initial_prompt, repair_prompt = (call.prompt for call in h.adapter.calls)
    expected_initial = render_initial_prompt(metadata())
    expected_repair = render_repair_prompt(metadata(), first.errors)
    assert initial_prompt == expected_initial.text
    assert repair_prompt == expected_repair.text
    assert expected_repair.metadata_digest == expected_initial.metadata_digest
    assert h.adapter.counted_prompts == [initial_prompt, repair_prompt]
    for leaked in (CANARY, SECRET_SQL, "note", "card_number"):
        assert leaked not in repair_prompt  # nothing from the raw initial candidate


def test_two_invalid_candidates_are_the_terminal_plan_invalid() -> None:
    h = Harness(adapter=_answers(INVALID_TEXT, UNKNOWN_FIELD_TEXT))
    assert h.gateway.initial().status is PlanningCallStatus.INVALID
    result = h.gateway.repair()
    assert result.status is PlanningCallStatus.PLAN_INVALID
    assert result.code is P.PLAN_INVALID and result.stage is PlanningStage.REPAIR
    assert result.errors and result.proposal is None
    assert all(isinstance(error, PlanningValidationError) for error in result.errors)
    assert CANARY not in repr(result) and SECRET_SQL not in repr(result)
    assert result.usage.closed_reason == "completed"
    assert result.usage.verified_model_settlements == 2
    assert h.gateway.closed is P.PLAN_INVALID
    for call in (h.gateway.initial, h.gateway.repair):
        _refused(call(), C.GATEWAY_CLOSED)
    assert h.model_calls == 2 and h.counts == 2


def test_a_valid_initial_plan_needs_no_repair_and_refuses_one() -> None:
    h = Harness()
    assert h.gateway.initial().status is PlanningCallStatus.PROPOSED
    before = _usage_counts(h)
    _refused(h.gateway.repair(), P.SEQUENCE_VIOLATION)
    assert h.model_calls == 1 and h.counts == 1
    assert _usage_counts(h) == before


# --- every illegal sequence is terminal and allocates nothing ---------------------------------


def _valid_initial(h: Harness) -> None:
    assert h.gateway.initial().status is PlanningCallStatus.PROPOSED


def _invalid_initial(h: Harness) -> None:
    assert h.gateway.initial().status is PlanningCallStatus.INVALID


def _invalid_then_valid(h: Harness) -> None:
    _invalid_initial(h)
    assert h.gateway.repair().status is PlanningCallStatus.PROPOSED


def _invalid_twice(h: Harness) -> None:
    _invalid_initial(h)
    assert h.gateway.repair().status is PlanningCallStatus.PLAN_INVALID


def _cancelled(h: Harness) -> None:
    h.cancellation.cancel_requested = True
    _refused(h.gateway.initial(), C.CANCELLED)


def _timed_out(h: Harness) -> None:
    h.clock.advance(PLANNING_LIMITS.wall_clock_seconds)
    _refused(h.gateway.initial(), C.BUDGET_EXHAUSTED)


def _failed(h: Harness) -> None:
    def boom(_adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        raise RuntimeError(CANARY)

    h.adapter.script = boom
    _refused(h.gateway.initial(), P.PROVIDER_FAILURE)


@pytest.mark.parametrize(
    ("answers", "setup", "call", "code", "calls"),
    [
        ((), None, "repair", P.SEQUENCE_VIOLATION, 0),
        ((), _valid_initial, "repair", P.SEQUENCE_VIOLATION, 1),
        ((), _valid_initial, "initial", P.SEQUENCE_VIOLATION, 1),
        ((INVALID_TEXT,), _invalid_initial, "initial", P.SEQUENCE_VIOLATION, 1),
        ((INVALID_TEXT, valid_response_text()), _invalid_then_valid, "repair", None, 2),
        ((INVALID_TEXT, valid_response_text()), _invalid_then_valid, "initial", None, 2),
        ((INVALID_TEXT, INVALID_TEXT), _invalid_twice, "repair", C.GATEWAY_CLOSED, 2),
        ((INVALID_TEXT, INVALID_TEXT), _invalid_twice, "initial", C.GATEWAY_CLOSED, 2),
        ((), _cancelled, "repair", C.GATEWAY_CLOSED, 0),
        ((), _timed_out, "repair", C.GATEWAY_CLOSED, 0),
        ((), _failed, "repair", C.GATEWAY_CLOSED, 1),
        ((), _failed, "initial", C.GATEWAY_CLOSED, 1),
    ],
    ids=[
        "repair-before-initial",
        "repair-after-valid-initial",
        "initial-twice-after-valid",
        "initial-twice-after-invalid",
        "third-call-repair-after-valid-repair",
        "third-call-initial-after-valid-repair",
        "repair-after-plan-invalid",
        "initial-after-plan-invalid",
        "repair-after-cancellation",
        "repair-after-timeout",
        "repair-after-failure",
        "initial-after-failure",
    ],
)
def test_illegal_sequences_are_refused_without_any_new_allowance(
    answers: tuple[str, ...],
    setup: Callable[[Harness], None] | None,
    call: str,
    code: object,
    calls: int,
) -> None:
    h = Harness(adapter=_answers(*answers))
    if setup is not None:
        setup(h)
    before = _usage_counts(h)
    result = getattr(h.gateway, call)()
    _refused(result, code or P.SEQUENCE_VIOLATION)
    assert h.model_calls == calls
    assert _usage_counts(h) == before  # no reservation, release, or charge
    assert h.gateway.usage().closed_reason is not None
    _refused(getattr(h.gateway, call)(), C.GATEWAY_CLOSED)
    assert h.model_calls == calls


def test_a_sequence_violation_after_an_invalid_initial_destroys_the_repair() -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    _refused(h.gateway.initial(), P.SEQUENCE_VIOLATION)
    _refused(h.gateway.repair(), C.GATEWAY_CLOSED)
    assert h.model_calls == 1


# --- the repair is bound to the initial call's inputs, metadata, and feedback -----------------


def test_a_changed_context_or_metadata_digest_is_refused_before_counting() -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT))
    _tamper(h.gateway, _context=context(question="A different question entirely"))
    _refused(h.gateway.repair(), C.AUTHORITY_UNAVAILABLE)
    assert h.counts == 1 and h.model_calls == 1

    h = _repairable(adapter=_answers(INVALID_TEXT))
    _tamper(h.gateway, _metadata_digest="0" * 64)
    before = _usage_counts(h)
    _refused(h.gateway.repair(), C.STATE_INVALID)
    assert h.counts == 1 and h.model_calls == 1
    assert _usage_counts(h) == before


@pytest.mark.parametrize(
    "feedback",
    [
        (PlanningValidationError.model_construct(code=CANARY, task_id=None),),
        (PlanningValidationError.model_construct(code="unknown_field", task_id=f"x {CANARY}"),),
        (f'{{"plan": "{CANARY}"}}',),
        (),
    ],
    ids=["forged-code", "forged-task-id", "raw-text", "empty"],
)
def test_forged_or_mutated_feedback_is_refused_before_counting(feedback: tuple[Any, ...]) -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT))
    _tamper(h.gateway, _feedback=feedback)
    result = h.gateway.repair()
    _refused(result, C.STATE_INVALID)
    assert h.counts == 1 and h.model_calls == 1
    assert CANARY not in repr(result)


@pytest.mark.parametrize(
    ("live_change", "code"),
    [
        ({"status": AuthorityStatus.REVOKED}, C.AUTHORITY_REVOKED),
        ({"error": RuntimeError(CANARY)}, C.AUTHORITY_UNAVAILABLE),
        ({"status": "expired"}, C.AUTHORITY_UNAVAILABLE),
    ],
    ids=["revoked", "unavailable", "expired"],
)
def test_authority_revoked_or_expired_between_calls_refuses_the_repair(
    live_change: dict[str, Any], code: RefusalCode
) -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    for name, value in live_change.items():
        setattr(h.live, name, value)
    before = _usage_counts(h)
    _refused(h.gateway.repair(), code)
    assert h.counts == 1 and h.model_calls == 1
    assert _usage_counts(h) == before
    assert h.gateway.usage().closed_reason == "policy_refused"


def test_consent_or_authority_changed_between_calls_refuses_the_repair() -> None:
    hosted = BOUND.model_copy(update={"data_recipient": "hosted"})
    accepted = ConsentSnapshot(
        accepted_at="2026-10-01T00:00:00Z", notice_version="v1", recipients=("hosted",)
    )
    granted = authority(
        consent=accepted,
        required_recipients=("hosted",),
        provider_envelope=ProviderEnvelope(members=(hosted,)),
    )
    adapter = _answers(INVALID_TEXT, valid_response_text(), declared=hosted)
    h = Harness(adapter=adapter, authority_value=granted, evidence_value=evidence(hosted))
    assert h.gateway.initial().status is PlanningCallStatus.INVALID

    # A consent change re-issues authority: the bound authority no longer matches.
    _tamper(h.gateway, _authority=granted.model_copy(update={"consent": ConsentSnapshot()}))
    _refused(h.gateway.repair(), C.AUTHORITY_UNAVAILABLE)
    assert h.model_calls == 1 and len(h.live.calls) == 1

    # The trusted adapter reports the old epoch as revoked after a consent change.
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    h.live.status = AuthorityStatus.REVOKED
    _refused(h.gateway.repair(), C.AUTHORITY_REVOKED)
    assert h.model_calls == 1


@pytest.mark.parametrize(
    "change",
    [
        {"_evidence": None},
        {"_evidence": evidence(BOUND.model_copy(update={"model": "planner-v2"}))},
        {"_pricing": None},
        {"_adapter": object()},
    ],
    ids=["evidence-removed", "evidence-replaced", "pricing-changed", "adapter-swapped"],
)
def test_changed_provider_admission_inputs_refuse_the_repair(change: dict[str, Any]) -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    adapter = h.adapter
    _tamper(h.gateway, **change)
    result = h.gateway.repair()
    assert result.status is PlanningCallStatus.REFUSED
    assert result.code in {C.AUTHORITY_UNAVAILABLE, C.METERING_UNBOUNDED}
    assert len(adapter.calls) == 1 and len(adapter.counted_prompts) == 1


# --- cancellation and deadline across the two calls ---------------------------------------------


def test_cancellation_before_the_repair_refuses_without_counting() -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    h.cancellation.cancel_requested = True
    _refused(h.gateway.repair(), C.CANCELLED)
    assert h.counts == 1 and h.model_calls == 1
    assert h.gateway.usage().closed_reason == "cancelled"


def test_cancellation_after_the_repair_count_releases_its_reservation() -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    original = h.adapter.count_input_tokens

    def count_then_cancel(prompt: str) -> int:
        h.cancellation.cancel_requested = True
        return original(prompt)

    h.adapter.count_input_tokens = count_then_cancel  # type: ignore[method-assign]
    _refused(h.gateway.repair(), C.CANCELLED)
    usage = h.gateway.usage()
    assert h.model_calls == 1 and usage.released_reservations == 1
    assert (usage.model_calls, usage.input_tokens) == (1, 1_234)


def test_cancellation_during_the_repair_with_confirmed_provider_stop() -> None:
    def cancel_and_stop(_adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        h.cancellation.cancel_requested = True
        return stopped(DeadlineTermination.PROVIDER_CANCELLED)

    adapter = ScriptedDeadlineAdapter(script=_per_call(INVALID_FIRST, cancel_and_stop))
    h = _repairable(adapter=adapter)
    _refused(h.gateway.repair(), C.CANCELLED)
    usage = h.gateway.usage()
    assert usage.model_calls == 2 and usage.unmetered_model_calls == 1
    assert usage.closed_reason == "cancelled"


def test_a_valid_repair_answer_arriving_after_cancellation_is_discarded() -> None:
    def cancel_then_answer(adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        h.cancellation.cancel_requested = True
        return _answer_with(valid_response_text())(adapter, _call)

    adapter = ScriptedDeadlineAdapter(script=_per_call(INVALID_FIRST, cancel_then_answer))
    h = _repairable(adapter=adapter)
    result = h.gateway.repair()
    _refused(result, C.CANCELLED)
    assert result.proposal is None


def test_cancellation_after_a_completed_repair_changes_nothing_and_allows_nothing() -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    assert h.gateway.repair().status is PlanningCallStatus.PROPOSED
    h.cancellation.cancel_requested = True
    before = _usage_counts(h)
    _refused(h.gateway.repair(), P.SEQUENCE_VIOLATION)
    assert _usage_counts(h) == before and h.model_calls == 2


def test_deadline_expiry_between_calls_is_budget_exhaustion_before_counting() -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    h.clock.advance(PLANNING_LIMITS.wall_clock_seconds)
    before = _usage_counts(h)
    _refused(h.gateway.repair(), C.BUDGET_EXHAUSTED)
    assert h.counts == 1 and h.model_calls == 1
    assert _usage_counts(h) == before
    assert h.gateway.usage().closed_reason == "exhausted"


def test_the_repair_deadline_is_what_remains_of_the_original_thirty_seconds() -> None:
    clock = FakeClock()

    def slow_invalid(adapter: ScriptedDeadlineAdapter, call: Call) -> DeadlineModelResult:
        clock.advance(18.0)
        return INVALID_FIRST(adapter, call)

    adapter = ScriptedDeadlineAdapter(
        script=_per_call(slow_invalid, _answer_with(valid_response_text()))
    )
    h = _repairable(adapter=adapter, clock=clock)
    clock.advance(4.0)
    assert h.gateway.repair().status is PlanningCallStatus.PROPOSED
    first, second = (call.deadline.remaining_seconds for call in adapter.calls)
    assert first == PLANNING_LIMITS.wall_clock_seconds
    assert second == pytest.approx(PLANNING_LIMITS.wall_clock_seconds - 22.0)


def test_a_provider_enforced_deadline_during_the_repair_is_terminal() -> None:
    def time_out(_adapter: ScriptedDeadlineAdapter, call: Call) -> DeadlineModelResult:
        h.clock.advance(call.deadline.remaining_seconds)
        return stopped(DeadlineTermination.PROVIDER_DEADLINE_ENFORCED)

    h = _repairable(adapter=ScriptedDeadlineAdapter(script=_per_call(INVALID_FIRST, time_out)))
    _refused(h.gateway.repair(), C.BUDGET_EXHAUSTED)
    assert h.gateway.usage().closed_reason == "exhausted"
    _refused(h.gateway.repair(), C.GATEWAY_CLOSED)
    assert h.model_calls == 2


def test_a_late_valid_repair_never_becomes_a_proposal() -> None:
    def late(adapter: ScriptedDeadlineAdapter, call: Call) -> DeadlineModelResult:
        h.clock.advance(call.deadline.remaining_seconds + 0.5)
        return _answer_with(valid_response_text())(adapter, call)

    h = _repairable(adapter=ScriptedDeadlineAdapter(script=_per_call(INVALID_FIRST, late)))
    result = h.gateway.repair()
    _refused(result, C.METERING_VIOLATION)
    assert result.proposal is None
    assert h.gateway.usage().verified_model_settlements == 1  # only the initial call


def test_an_unconfirmed_repair_timeout_is_a_violation() -> None:
    def local_stop(_adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        return stopped(DeadlineTermination.PROVIDER_DEADLINE_ENFORCED, confirmed=False)

    h = _repairable(adapter=ScriptedDeadlineAdapter(script=_per_call(INVALID_FIRST, local_stop)))
    _refused(h.gateway.repair(), C.METERING_VIOLATION)


# --- metering and provider contract on the second call ------------------------------------------


@pytest.mark.parametrize(
    "second",
    [
        _answer_with(valid_response_text(), identity=("scripted", "planner-v2")),
        _answer_with(valid_response_text(), identity=("other", "planner-v1")),
        _answer_with(valid_response_text(), served=(("other", "planner-v1"),)),
        _answer_with(valid_response_text(), reports=()),
        _answer_with(valid_response_text(), reports=({"prompt_tokens": 1_234},)),
        _answer_with(valid_response_text(), reports=({"prompt_tokens": 99, "output_tokens": 1},)),
        _answer_with(
            valid_response_text(), reports=({"prompt_tokens": 1_234, "output_tokens": 1_001},)
        ),
        _answer_with(
            valid_response_text(),
            reports=({"prompt_tokens": 1_234, "output_tokens": 321, "retries": 1},),
        ),
        _answer_with(
            valid_response_text(),
            reports=(
                {"prompt_tokens": 1_234, "output_tokens": 321},
                {"prompt_tokens": 1_234, "output_tokens": 321},
            ),
        ),
        _answer_with(valid_response_text(), fallbacks=1),
    ],
    ids=[
        "wrong-model",
        "wrong-provider",
        "served-elsewhere",
        "missing-report",
        "partial-report",
        "forged-report",
        "excessive-output",
        "hidden-retry",
        "second-report",
        "fallback",
    ],
)
def test_a_second_call_contract_breach_is_terminal_and_never_a_proposal(second: Any) -> None:
    h = _repairable(adapter=ScriptedDeadlineAdapter(script=_per_call(INVALID_FIRST, second)))
    result = h.gateway.repair()
    _refused(result, C.METERING_VIOLATION)
    usage = h.gateway.usage()
    assert usage.verified_model_settlements == 1  # the repair is never verified
    assert usage.input_tokens >= 1_234 + PLANNING_STAGE_INPUT_TOKENS
    assert usage.closed_reason in {"policy_refused", "reservation_exceeded"}
    assert usage.outstanding_reservations == 0
    _refused(h.gateway.repair(), C.GATEWAY_CLOSED)
    assert h.model_calls == 2


def test_hidden_retries_in_the_repair_are_charged_past_the_call_budget() -> None:
    second = _answer_with(
        valid_response_text(),
        reports=({"prompt_tokens": 1_234, "output_tokens": 321, "retries": 2},),
    )
    h = _repairable(adapter=ScriptedDeadlineAdapter(script=_per_call(INVALID_FIRST, second)))
    _refused(h.gateway.repair(), C.METERING_VIOLATION)
    usage = h.gateway.usage()
    assert usage.model_calls == 4  # charged as observed, never hidden as success
    assert usage.reservation_bound == "exceeded"
    assert usage.closed_reason == "reservation_exceeded"


def test_a_repair_meter_fault_is_charged_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))

    def broken(*_args: object) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(investigation_planning._CallMeter, "record_usage", broken)
    result = h.gateway.repair()
    _refused(result, C.METERING_VIOLATION)
    usage = h.gateway.usage()
    assert usage.unmetered_model_calls == 1
    assert usage.input_tokens == 1_234 + PLANNING_STAGE_INPUT_TOKENS
    assert CANARY not in repr(result)


def test_a_repair_provider_exception_is_a_content_free_failure() -> None:
    def boom(_adapter: ScriptedDeadlineAdapter, _call: Call) -> DeadlineModelResult:
        raise RuntimeError(f"{CANARY} {SECRET_SQL}")

    h = _repairable(adapter=ScriptedDeadlineAdapter(script=_per_call(INVALID_FIRST, boom)))
    result = h.gateway.repair()
    _refused(result, P.PROVIDER_FAILURE)
    assert CANARY not in repr(result) and SECRET_SQL not in repr(result)
    assert h.gateway.usage().unmetered_model_calls == 1


def test_an_unusable_repair_count_releases_and_closes() -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    h.adapter.count = 0
    _refused(h.gateway.repair(), C.METERING_UNBOUNDED)
    assert h.gateway.usage().released_reservations == 1
    assert h.model_calls == 1


def test_a_repair_over_its_input_partition_is_refused_before_invocation() -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    h.adapter.count = PLANNING_STAGE_INPUT_TOKENS + 1
    _refused(h.gateway.repair(), C.BUDGET_EXHAUSTED)
    assert h.model_calls == 1 and h.gateway.usage().released_reservations == 1


@pytest.mark.parametrize("stage", ["initial", "repair"])
def test_an_over_input_count_is_exhaustion_without_a_named_resource(stage: str) -> None:
    """Pins the documented S2e limitation: the count is never charged, so no resource is named.

    ``BudgetLedger`` names an exhausted resource only when an authorization or a settlement
    exceeds a limit. Neither happens here: the stage reservation fits and is released unused.
    Naming ``input_tokens`` would require charging tokens that were never sent.
    """
    answers = (INVALID_TEXT, valid_response_text())
    h = _repairable(adapter=_answers(*answers)) if stage == "repair" else Harness()
    h.adapter.count = PLANNING_STAGE_INPUT_TOKENS + 1
    _refused(getattr(h.gateway, stage)(), C.BUDGET_EXHAUSTED)
    usage = h.gateway.usage()
    assert usage.closed_reason == "exhausted" and usage.exhausted_resource is None
    assert usage.input_tokens == (1_234 if stage == "repair" else 0)
    assert usage.released_reservations == 1


# --- cumulative budget --------------------------------------------------------------------------


def test_two_maximal_stages_exactly_fill_the_planning_budget() -> None:
    adapter = _answers(
        INVALID_TEXT,
        valid_response_text(),
        count=PLANNING_STAGE_INPUT_TOKENS,
        output_tokens=PLANNING_STAGE_OUTPUT_TOKENS,
    )
    h = _repairable(adapter=adapter)
    result = h.gateway.repair()
    assert result.status is PlanningCallStatus.PROPOSED
    usage = result.usage
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (
        PLANNING_LIMITS.model_calls,
        PLANNING_LIMITS.input_tokens,
        PLANNING_LIMITS.output_tokens,
    )
    assert (usage.sql_executions, usage.retrievals, usage.task_attempts) == (0, 0, 0)


def test_an_exhausted_cumulative_budget_refuses_the_repair_before_counting() -> None:
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    # Something else on the same ledger already holds the second model call. Tokens can only be
    # reserved with a model call, so the call budget is what a third reservation would exceed.
    _occupy_ledger(h.gateway, BudgetRequest.model_call(input_tokens=1, output_tokens=1))
    _refused(h.gateway.repair(), C.BUDGET_EXHAUSTED)
    assert h.counts == 1 and h.model_calls == 1
    usage = h.gateway.usage()
    assert usage.closed_reason == "exhausted"
    assert usage.exhausted_resource is not None and usage.exhausted_resource.value == "model_calls"


def test_the_repair_reserves_exactly_one_fixed_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    reserved: list[PlanningStage] = []
    original = investigation_planning.reserve_planning_stage

    def spy(ledger: Any, stage: PlanningStage) -> Any:
        reserved.append(stage)
        return original(ledger, stage)

    monkeypatch.setattr(investigation_planning, "reserve_planning_stage", spy)
    h = _repairable(adapter=_answers(INVALID_TEXT, valid_response_text()))
    assert h.gateway.repair().status is PlanningCallStatus.PROPOSED
    assert reserved == [PlanningStage.INITIAL, PlanningStage.REPAIR]


# --- tools, content, and concurrency ------------------------------------------------------------


def test_a_repair_requesting_tools_is_plan_invalid_and_runs_nothing(
    no_tools_or_network: list[str],
) -> None:
    tool_text = json.dumps(
        {
            **json.loads(valid_response_text()),
            "tool_call": {"name": "execute_sql", "arguments": {"sql": SECRET_SQL}},
        }
    )
    h = Harness(adapter=_answers(INVALID_TEXT, tool_text))
    assert h.gateway.initial().status is PlanningCallStatus.INVALID
    result = h.gateway.repair()
    assert result.status is PlanningCallStatus.PLAN_INVALID
    assert SECRET_SQL not in repr(result)
    assert no_tools_or_network == []


def test_a_repair_cannot_grant_approval_scope_budget_or_roles() -> None:
    forged = json.loads(valid_response_text())
    forged["plan"]["tasks"][0]["assignee"] = "supervisor"
    for extra in ({"approved": True}, {"scope": {"tenant_id": "other"}}, {"budget": {"x": 1}}):
        h = Harness(adapter=_answers(INVALID_TEXT, json.dumps({**forged, **extra})))
        h.gateway.initial()
        result = h.gateway.repair()
        assert result.status is PlanningCallStatus.PLAN_INVALID
        assert h.gateway.usage().closed_reason == "completed"


def test_a_provider_calling_back_into_the_gateway_during_the_repair_fails_closed() -> None:
    inner: list[PlanningCallResult] = []

    def reenter(adapter: ScriptedDeadlineAdapter, call: Call) -> DeadlineModelResult:
        inner.append(h.gateway.repair())
        inner.append(h.gateway.initial())
        return _answer_with(valid_response_text())(adapter, call)

    h = _repairable(adapter=ScriptedDeadlineAdapter(script=_per_call(INVALID_FIRST, reenter)))
    outer = h.gateway.repair()
    for result in inner:
        _refused(result, C.CONCURRENT_CALL)
    _refused(outer, C.GATEWAY_CLOSED)
    assert h.model_calls == 2
    usage = h.gateway.usage()
    assert usage.outstanding_reservations == 0 and usage.model_calls == 2


def test_a_provider_calling_repair_during_the_initial_call_fails_closed() -> None:
    inner: list[PlanningCallResult] = []

    def reenter(adapter: ScriptedDeadlineAdapter, call: Call) -> DeadlineModelResult:
        inner.append(h.gateway.repair())
        return INVALID_FIRST(adapter, call)

    h = Harness(adapter=ScriptedDeadlineAdapter(script=reenter))
    outer = h.gateway.initial()
    _refused(inner[0], C.CONCURRENT_CALL)
    _refused(outer, C.GATEWAY_CLOSED)
    _refused(h.gateway.repair(), C.GATEWAY_CLOSED)
    assert h.model_calls == 1


def test_a_concurrent_thread_cannot_start_a_repair_while_one_is_in_flight() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking(adapter: ScriptedDeadlineAdapter, call: Call) -> DeadlineModelResult:
        entered.set()
        assert release.wait(timeout=5)
        return _answer_with(valid_response_text())(adapter, call)

    h = _repairable(adapter=ScriptedDeadlineAdapter(script=_per_call(INVALID_FIRST, blocking)))
    outcome: list[PlanningCallResult] = []
    worker = threading.Thread(target=lambda: outcome.append(h.gateway.repair()))
    worker.start()
    assert entered.wait(timeout=5)
    try:
        _refused(h.gateway.repair(), C.CONCURRENT_CALL)
        _refused(h.gateway.initial(), C.CONCURRENT_CALL)
    finally:
        release.set()
        worker.join(timeout=5)
    _refused(outcome[0], C.GATEWAY_CLOSED)
    assert h.model_calls == 2
    assert h.gateway.usage().outstanding_reservations == 0


# --- end to end: the INV-2 synthetic churn investigation ----------------------------------------

_SAMPLE = ROOT / "sample_data"
_TYPES = {
    "tenure_months_start": "integer",
    "monthly_price": "number",
    "mrr_at_start": "number",
    "payment_failures_90d": "integer",
    "support_tickets": "integer",
    "avg_resolution_hours": "number",
    "active_days_last_30": "integer",
    "sessions_last_30": "integer",
    "active_at_start": "boolean",
    "renewal_due": "boolean",
    "active_at_end": "boolean",
    "churned": "boolean",
}


def _churn_metadata() -> PlanningMetadata:
    """Approved model-visible metadata only: the CSV header and the PDF display titles."""
    with (_SAMPLE / "subscription_churn.csv").open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    titles = sorted(
        path.stem.removeprefix("subscription_churn_").replace("_", " ").capitalize()
        for path in _SAMPLE.glob("subscription_churn_*.pdf")
    )
    public = json.loads((ROOT / "evals" / "investigation" / "v1" / "public_cases.json").read_text())
    question = next(c["question"] for c in public["cases"] if c["id"] == "churn-full-investigation")
    return PlanningMetadata(
        question=question,
        schema_fields=tuple(
            SchemaFieldMetadata(name=name, logical_type=_TYPES.get(name, "text")) for name in header
        ),
        document_titles=tuple(DocumentTitleMetadata(title=title) for title in titles),
    )


def _churn_plan(segment_field: str) -> InvestigationPlan:
    return plan_of(
        sql_task("trend", fields=("quarter", "churned")),
        sql_task("segments", depends_on=("trend",), fields=("quarter", segment_field, "churned")),
        document_task("pricing"),
    )


def test_end_to_end_churn_planning_repairs_an_unknown_field_once() -> None:
    meta = _churn_metadata()
    churn_context = PlanningContext(scope=scope(), metadata=meta)
    initial_text = json.dumps(
        {"plan": _churn_plan("segment").model_dump(mode="json"), "note_to_self": CANARY}
    )
    repaired = json.dumps(
        {
            "plan": _churn_plan("customer_segment").model_dump(mode="json"),
            "missing_information": ["Retention outcomes after the playbook are not recorded."],
        }
    )
    h = Harness(adapter=_answers(initial_text, repaired), context_value=churn_context)
    first = h.gateway.initial()
    assert first.status is PlanningCallStatus.INVALID
    assert [error.code for error in first.errors] == ["planning_draft_invalid"]

    result = h.gateway.repair()
    assert result.status is PlanningCallStatus.PROPOSED
    assert result.proposal is not None
    assert result.proposal.plan == _churn_plan("customer_segment")
    assert {req.kind for req in result.proposal.approval_requirements} == {"plan", "statement_sql"}

    initial_prompt, repair_prompt = (call.prompt for call in h.adapter.calls)
    assert repair_prompt == render_repair_prompt(meta, first.errors).text
    assert CANARY not in repair_prompt
    for title in meta.document_titles:
        assert json.dumps(title.title)[1:-1] in initial_prompt
    for forbidden in ("CUS-00001", "CQ-00001", "oracle", "reference.sql", "49.00"):
        assert forbidden not in initial_prompt and forbidden not in repair_prompt
    assert result.usage.model_calls == 2 and result.usage.closed_reason == "completed"


def test_end_to_end_churn_unknown_field_twice_is_plan_invalid_with_task_ids() -> None:
    meta = _churn_metadata()
    churn_context = PlanningContext(scope=scope(), metadata=meta)
    wrong = json.dumps({"plan": _churn_plan("segment").model_dump(mode="json")})
    h = Harness(adapter=_answers(wrong, wrong), context_value=churn_context)
    first = h.gateway.initial()
    assert first.status is PlanningCallStatus.INVALID
    assert ("unknown_field", "segments") in {(e.code, e.task_id) for e in first.errors}
    repair_prompt_data = json.loads(render_repair_prompt(meta, first.errors).data)[
        "validation_feedback"
    ]
    assert {"code": "unknown_field", "task_id": "segments"} in repair_prompt_data
    result = h.gateway.repair()
    assert result.status is PlanningCallStatus.PLAN_INVALID
    assert h.adapter.calls[1].prompt == render_repair_prompt(meta, first.errors).text


def test_the_runtime_gateway_reads_no_evaluator_or_sample_files() -> None:
    source = (ROOT / "src" / "orchestration" / "investigation_planning.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("evals", "oracle", "sample_data", "reference.sql", "open("):
        assert forbidden not in source
