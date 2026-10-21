"""INV-7 S3 (#140): the gateway's owner-only review calls, round opening, and follow-up checks."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from packages.investigation import RefusalCode
from packages.investigation.budget import EXECUTION_LIMITS, BudgetLedger, BudgetLimits
from packages.investigation.contracts import AgentRole
from packages.investigation.state import restore_state
from src.llm.base import LLMGenerationError
from src.orchestration.review_prompts import (
    CRITIC_MARKER,
    CRITIC_PROMPT_VERSION,
    FINDINGS_MARKER,
    FINDINGS_PROMPT_VERSION,
)
from src.orchestration.tool_gateway import (
    BeginAttempt,
    DraftFindings,
    InvestigationToolGateway,
    OpenFollowUpRound,
    ReviewFindings,
    _sql_question,
)
from tests.investigation_churn_schema import churn_v2_plan
from tests.investigation_critique_fakes import (
    admission,
    admitted_state,
    approval_for,
    critique,
    stratified_follow_up,
    v2_running_state,
)
from tests.investigation_fixtures import apply_update, task_record
from tests.test_investigation_tool_gateway import (
    MAX_INPUT,
    Cancellation,
    Clock,
    ExactAdapter,
    Harness,
    LiveAuthority,
    SpyResolver,
    SpyRetriever,
    _gemini,
    exact_authority,
)

DRAFT = json.dumps({"claims": []})
REVIEW = json.dumps(
    {
        "gaps": [
            {
                "hypothesis_id": "h1",
                "test_task_id": "h1_rate",
                "field": "tenure_months_start",
                "cut_points": [6],
            }
        ]
    }
)
NUMERIC = frozenset({"tenure_months_start", "support_tickets"})


def _harness(
    *,
    llm: Any = None,
    state: dict[str, Any] | None = None,
    limits: BudgetLimits = EXECUTION_LIMITS,
    max_input: int = MAX_INPUT,
) -> Harness:
    plan = churn_v2_plan()
    retriever = SpyRetriever()
    resolver = SpyResolver(retriever)
    llm = ExactAdapter(text=DRAFT) if llm is None else llm
    live, clock, cancellation = LiveAuthority(), Clock(), Cancellation()
    ledger = BudgetLedger(limits, clock=lambda: 0.0)
    gateway = InvestigationToolGateway(
        authority=exact_authority(plan=plan),
        live=live,
        clock=clock,
        cancellation=cancellation,
        ledger=ledger,
        resolver=resolver,
        llm_client=llm,
        max_input_tokens_per_call=max_input,
        numeric_fields=NUMERIC,
    )
    return Harness(
        gateway=gateway,
        retriever=retriever,
        resolver=resolver,
        llm=llm,
        live=live,
        clock=clock,
        cancellation=cancellation,
        ledger=ledger,
        state=state if state is not None else v2_running_state(),
    )


def _draft(h: Harness, round_index: int = 0, role: AgentRole = AgentRole.BUSINESS_INVESTIGATOR):
    return h.call("draft_findings", DraftFindings(role=role, round=round_index))


def _review(h: Harness, round_index: int = 0, role: AgentRole = AgentRole.CRITIC):
    return h.call("review_findings", ReviewFindings(role=role, round=round_index))


def _open(h: Harness, tasks: list[Any] | None = None, **approval: Any):
    tasks = tasks or [stratified_follow_up()]
    granted = approval_for(tasks, 1, **approval)
    request = OpenFollowUpRound(
        role=AgentRole.SUPERVISOR,
        round=1,
        follow_up_digest=granted.follow_up_digest,
        approval=granted,
    )
    return h.call("open_follow_up_round", request)


def _reviewed_state() -> dict[str, Any]:
    return apply_update(v2_running_state(), "critic", {"critiques": [critique(0)]})


# --- the draft ----------------------------------------------------------------------------------


def test_a_draft_is_one_metered_model_call_whose_output_stays_in_process() -> None:
    h = _harness(
        llm=ExactAdapter(
            text=json.dumps(
                {
                    "claims": [
                        {
                            "hypothesis_id": "h1",
                            "basis": "data",
                            "evidence_ids": ["ev_1"],
                            "statement": "CLAIM-CANARY-4411",
                        }
                    ]
                }
            )
        )
    )
    result = _draft(h)
    assert result.refusal is None and result.review_status == "valid"
    assert result.draft is not None and result.draft.claims[0].hypothesis_id == "h1"
    view = restore_state(h.state)
    decision = view.decisions[-1]
    assert (decision.tool, decision.role, decision.task_id, decision.result) == (
        "draft_findings",
        AgentRole.BUSINESS_INVESTIGATOR,
        None,
        "succeeded",
    )
    usage = view.budget_usage
    assert usage is not None and usage.model_calls == 1 and usage.critic_rounds == 0
    assert "CLAIM-CANARY-4411" not in json.dumps(h.state)  # the claim is never recorded
    (prompt,) = h.llm.prompts
    assert prompt.startswith(f"{FINDINGS_MARKER} ({FINDINGS_PROMPT_VERSION})")
    assert "SMB Basic churn rose contemporaneously" in prompt  # the approved statement


def test_malformed_draft_output_is_invalid_and_never_retried() -> None:
    h = _harness(llm=ExactAdapter(text="Sure! Here are the claims: ..."))
    result = _draft(h)
    assert (result.refusal, result.draft, result.review_status) == (None, None, "invalid")
    assert len(h.llm.prompts) == 1
    assert restore_state(h.state).decisions[-1].result == "succeeded"


def test_a_provider_failure_makes_the_draft_unavailable() -> None:
    h = _harness(llm=ExactAdapter(error=LLMGenerationError("provider exploded")))
    result = _draft(h)
    assert (result.refusal, result.review_status) == (None, "unavailable")
    decision = restore_state(h.state).decisions[-1]
    assert (decision.result, decision.category) == ("failed", "provider_failure")


def test_a_prompt_over_the_input_cap_is_never_sent_and_charges_nothing() -> None:
    h = _harness(max_input=50)
    result = _draft(h)
    assert (result.refusal, result.review_status) == (None, "unavailable")
    assert h.llm.prompts == []
    usage = restore_state(h.state).budget_usage
    assert usage is not None and usage.model_calls == 0 and usage.released_reservations == 1
    assert h.gateway.closed is None
    assert restore_state(h.state).decisions[-1].category == "invalid_input"


def test_the_draft_is_for_the_round_under_review_only() -> None:
    h = _harness()
    result = _draft(h, round_index=1)
    assert result.refusal is RefusalCode.INVALID_REQUEST and h.llm.prompts == []
    assert h.gateway.closed is None


def test_only_the_business_investigator_drafts_and_only_the_critic_reviews() -> None:
    h = _harness()
    assert _draft(h, role=AgentRole.CRITIC).refusal is RefusalCode.ROLE_NOT_PERMITTED
    assert h.gateway.closed is RefusalCode.ROLE_NOT_PERMITTED  # a role violation fails closed
    h = _harness()
    assert _review(h, role=AgentRole.SUPERVISOR).refusal is RefusalCode.ROLE_NOT_PERMITTED
    assert h.llm.prompts == []


def test_a_production_client_is_refused_before_any_call() -> None:
    h = _harness(llm=_gemini())
    assert _draft(h).refusal is RefusalCode.METERING_UNBOUNDED
    assert h.gateway.closed is RefusalCode.METERING_UNBOUNDED


def test_a_late_answer_after_cancellation_is_discarded() -> None:
    h = _harness()
    h.llm.during = lambda: setattr(h.cancellation, "cancel_requested", True)
    result = _draft(h)
    assert result.refusal is RefusalCode.CANCELLED and result.draft is None
    assert len(h.llm.prompts) == 1  # the call in flight finished; its answer was discarded
    assert h.gateway.closed is RefusalCode.CANCELLED


# --- the Critic review --------------------------------------------------------------------------


def test_a_review_charges_one_critic_round_and_one_model_call() -> None:
    h = _harness(llm=ExactAdapter(text=REVIEW))
    result = _review(h)
    assert result.refusal is None and result.review_status == "valid"
    assert result.review is not None and result.review.gaps[0].field == "tenure_months_start"
    usage = restore_state(h.state).budget_usage
    assert usage is not None and (usage.critic_rounds, usage.model_calls) == (1, 1)
    (prompt,) = h.llm.prompts
    assert prompt.startswith(f"{CRITIC_MARKER} ({CRITIC_PROMPT_VERSION})")
    assert "Numeric fields: support_tickets, tenure_months_start" in prompt
    assert "h1_rate (h1, untestable): rate of churned over active_at_start" in prompt
    assert "statement" not in prompt and "contemporaneously" not in prompt


def test_the_critic_round_budget_is_enforced_by_the_ledger() -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"critic_rounds": 0})
    h = _harness(llm=ExactAdapter(text=REVIEW), limits=limits)
    assert _review(h).refusal is RefusalCode.BUDGET_EXHAUSTED
    assert h.llm.prompts == [] and h.gateway.closed is RefusalCode.BUDGET_EXHAUSTED


def test_a_review_proposing_too_many_gaps_is_invalid() -> None:
    gaps = json.loads(REVIEW)["gaps"] * 4
    h = _harness(llm=ExactAdapter(text=json.dumps({"gaps": gaps})))
    result = _review(h)
    assert (result.review, result.review_status) == (None, "invalid")


# --- opening a follow-up round ------------------------------------------------------------------


def test_an_approved_round_is_opened_charged_and_then_admitted() -> None:
    h = _harness(state=_reviewed_state())
    result = _open(h)
    assert result.refusal is None
    usage = restore_state(h.state).budget_usage
    assert usage is not None and usage.replan_rounds == 1
    admitted = apply_update(h.state, "supervisor", admission([stratified_follow_up()]))
    assert [t.id for t in restore_state(admitted).admitted_follow_ups] == ["h1_tenure"]


@pytest.mark.parametrize(
    "case", ["digest", "round", "expired", "outside_window", "before_plan_approval"]
)
def test_an_unbound_or_expired_approval_is_refused(case: str) -> None:
    h = _harness(state=_reviewed_state())
    tasks = [stratified_follow_up()]
    granted = approval_for(tasks, 1)
    digest = granted.follow_up_digest
    if case == "digest":
        digest = "0" * 64
    elif case == "round":
        granted = approval_for(tasks, 2)
    elif case == "expired":
        h.clock.now_value = granted.deadline + timedelta(seconds=1)
    elif case == "outside_window":
        granted = approval_for(tasks, 1, hours=48)
    elif case == "before_plan_approval":
        granted = granted.model_copy(
            update={"approved_at": granted.approved_at - timedelta(days=1)}
        )
    request = OpenFollowUpRound(
        role=AgentRole.SUPERVISOR, round=1, follow_up_digest=digest, approval=granted
    )
    result = h.call("open_follow_up_round", request)
    assert result.refusal is RefusalCode.FOLLOW_UP_NOT_AUTHORIZED
    usage = restore_state(h.state).budget_usage
    assert usage is None or usage.replan_rounds == 0
    assert h.gateway.closed is None  # task severity: the investigation can still finish


def test_a_round_needs_an_admitting_critique_and_follows_round_order() -> None:
    h = _harness()
    assert _open(h).refusal is RefusalCode.INVALID_REQUEST
    declined = apply_update(
        v2_running_state(), "critic", {"critiques": [critique(0, replan="not_authorized")]}
    )
    assert _open(_harness(state=declined)).refusal is RefusalCode.INVALID_REQUEST
    assert _open(_harness(state=admitted_state())).refusal is RefusalCode.INVALID_REQUEST


def test_only_the_supervisor_opens_a_round_and_the_replan_budget_applies() -> None:
    h = _harness(state=_reviewed_state())
    granted = approval_for([stratified_follow_up()], 1)
    request = OpenFollowUpRound(
        role=AgentRole.CRITIC, round=1, follow_up_digest=granted.follow_up_digest, approval=granted
    )
    assert h.call("open_follow_up_round", request).refusal is RefusalCode.ROLE_NOT_PERMITTED
    limits = EXECUTION_LIMITS.model_copy(update={"replan_rounds": 0})
    assert _open(_harness(state=_reviewed_state(), limits=limits)).refusal is (
        RefusalCode.BUDGET_EXHAUSTED
    )


def test_a_third_round_cannot_be_requested() -> None:
    with pytest.raises(ValueError):
        OpenFollowUpRound(
            role=AgentRole.SUPERVISOR,
            round=3,
            follow_up_digest="0" * 64,
            approval=approval_for([stratified_follow_up()], 2),
        )


# --- follow-up tasks run only under their round's approval --------------------------------------


def _follow_up_ready() -> dict[str, Any]:
    state = admitted_state()
    state = apply_update(
        state, "dispatcher", {"tasks": {"h1_rate": task_record("h1_rate", "running", 1)}}
    )
    state = apply_update(
        state, "dispatcher", {"tasks": {"h1_rate": task_record("h1_rate", "succeeded", 1)}}
    )
    return apply_update(state, "dispatcher", {"tasks": {"h1_tenure": task_record("h1_tenure")}})


def test_a_follow_up_starts_while_its_approval_is_open() -> None:
    h = _harness(state=_follow_up_ready())
    result = h.call("begin_attempt", BeginAttempt(task_id="h1_tenure", role=AgentRole.SQL_ANALYST))
    assert result.refusal is None and result.attempt is not None


def test_a_follow_up_is_refused_once_its_approval_expires() -> None:
    h = _harness(state=_follow_up_ready())
    h.clock.now_value = h.clock.now_value + timedelta(hours=7)
    result = h.call("begin_attempt", BeginAttempt(task_id="h1_tenure", role=AgentRole.SQL_ANALYST))
    assert result.refusal is RefusalCode.FOLLOW_UP_NOT_AUTHORIZED
    record = restore_state(h.state).tasks["h1_tenure"]
    assert record.status == "refused" and record.outcome is not None
    assert h.gateway.closed is None


def test_the_stratified_question_names_the_bands_and_the_counts() -> None:
    task = stratified_follow_up()
    question = _sql_question(task, churn_v2_plan())
    assert "stratum as 'band_0' when tenure_months_start < 6" in question
    assert "'band_1' when tenure_months_start >= 6" in question
    assert "quarter_start, customer_segment, plan_tier, stratum" in question
    assert "numerator, denominator" in question
