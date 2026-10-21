"""INV-8 S4 (#141): the owner-only narrative call, its prompt, and strict parsing.

The narrative call is one bounded, metered model call made through the tool gateway. It is made
only after the final verdicts, once, and only for a run that would end ``completed`` or
``completed_partial``. Its prompt carries fact fields and fixed codes only; its output is an
untrusted typed draft, parsed strictly and never recorded.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from packages.investigation import RefusalCode
from packages.investigation.budget import EXECUTION_LIMITS, BudgetLimits
from packages.investigation.contracts import AgentRole, TaskKind
from packages.investigation.hypotheses import InvestigationPlanV2
from packages.investigation.report import NARRATIVE_PROMPT_VERSION as REPORT_VERSION
from packages.investigation.report import parse_narrative
from packages.investigation.state import VerdictRecord, restore_state
from src.llm.base import LLMGenerationError
from src.orchestration.report_prompt import (
    NARRATIVE_MARKER,
    NARRATIVE_PROMPT_VERSION,
    render_narrative_prompt,
)
from src.orchestration.tool_gateway import ComposeNarrative, DraftFindings
from tests.investigation_churn_schema import churn_v2_plan
from tests.investigation_critique_fakes import v2_running_state
from tests.investigation_fixtures import apply_update, running_state, task_record
from tests.test_investigation_review_gateway import _harness
from tests.test_investigation_tool_gateway import ExactAdapter, Harness, _gemini

VALID = json.dumps(
    {
        "sections": [
            {"name": "executive_summary", "claims": [{"type": "no_recommendation"}]},
            {"name": "findings", "claims": []},
        ]
    }
)


def ended_state(*, succeeded: bool = True, verdicts: bool = True) -> dict[str, Any]:
    """A v2 churn run after its tasks ended (still ``running``): context tasks succeeded, every
    test failed, so every hypothesis is ``untestable``; the run would end ``completed_partial``
    (or ``failed`` when nothing succeeded)."""
    plan = churn_v2_plan()
    assert isinstance(plan, InvestigationPlanV2)
    tasks = {
        task.id: task_record(
            task.id,
            "succeeded" if succeeded and task.kind is not TaskKind.HYPOTHESIS_TEST else "failed",
            1,
        )
        for task in plan.tasks
    }
    state = v2_running_state(**tasks)
    if verdicts:
        records = {
            item.id: VerdictRecord(
                hypothesis_id=item.id,
                verdict="untestable",
                reasons=(
                    ("required_data_absent",)
                    if item.testability == "untestable"
                    else ("test_not_completed",)
                ),
                test_task_ids=item.tests,
            ).model_dump(mode="json")
            for item in plan.hypotheses
        }
        state = apply_update(state, "investigator", {"verdicts": records})
    return state


def narrate(h: Harness, role: AgentRole = AgentRole.BUSINESS_INVESTIGATOR) -> Any:
    return h.call("compose_narrative", ComposeNarrative(role=role))


def test_one_bounded_call_returns_a_typed_draft_and_records_nothing_else() -> None:
    h = _harness(llm=ExactAdapter(text=VALID), state=ended_state())
    result = narrate(h)
    assert result.refusal is None and result.review_status == "valid"
    assert result.narrative is not None and result.narrative.sections[0].name == "executive_summary"
    view = restore_state(h.state)
    decision = view.decisions[-1]
    assert (decision.tool, decision.role, decision.task_id, decision.result) == (
        "compose_narrative",
        AgentRole.BUSINESS_INVESTIGATOR,
        None,
        "succeeded",
    )
    assert view.budget_usage is not None and view.budget_usage.model_calls == 1
    assert "no_recommendation" not in json.dumps(h.state)  # the draft is never recorded
    (prompt,) = h.llm.prompts
    assert prompt.startswith(f"{NARRATIVE_MARKER} ({NARRATIVE_PROMPT_VERSION})")


def test_the_prompt_carries_codes_and_fact_fields_only() -> None:
    h = _harness(llm=ExactAdapter(text=VALID), state=ended_state())
    narrate(h)
    (prompt,) = h.llm.prompts
    plan = churn_v2_plan()
    assert isinstance(plan, InvestigationPlanV2)
    for hypothesis in plan.hypotheses:
        assert hypothesis.statement not in prompt  # untrusted display text never reaches it
    assert plan.objective not in prompt
    for forbidden in ("SELECT", "tenant", "workspace", "Source 1", ".pdf", "budget"):
        assert forbidden not in prompt
    assert '"code":"causation_not_established"' in prompt
    assert '"status":"completed_partial"' in prompt  # the predicted terminal status


def test_the_prompt_versions_agree() -> None:
    assert NARRATIVE_PROMPT_VERSION == REPORT_VERSION == "inv8-narrative-prompt-v1"


def test_only_once_and_only_after_the_verdicts() -> None:
    h = _harness(llm=ExactAdapter(text=VALID), state=ended_state())
    assert narrate(h).refusal is None
    assert narrate(h).refusal is RefusalCode.INVALID_REQUEST
    early = _harness(llm=ExactAdapter(text=VALID), state=ended_state(verdicts=False))
    assert narrate(early).refusal is RefusalCode.INVALID_REQUEST
    assert early.llm.prompts == []


def test_only_the_business_investigator_on_a_v2_plan() -> None:
    h = _harness(llm=ExactAdapter(text=VALID), state=ended_state())
    assert narrate(h, AgentRole.CRITIC).refusal is RefusalCode.ROLE_NOT_PERMITTED
    v1 = _harness(llm=ExactAdapter(text=VALID), state=running_state())
    # Bound to the v2 plan's approval, a v1 state is refused before the plan check; either way
    # no narrative is drafted for a plan without hypotheses.
    assert narrate(v1).refusal in {RefusalCode.APPROVAL_MISMATCH, RefusalCode.TOOL_NOT_PERMITTED}
    assert h.llm.prompts == [] and v1.llm.prompts == []


def test_a_failing_run_gets_no_narrative_call() -> None:
    h = _harness(llm=ExactAdapter(text=VALID), state=ended_state(succeeded=False))
    assert narrate(h).refusal is RefusalCode.INVALID_REQUEST
    assert h.llm.prompts == []


def test_a_cancelled_run_gets_no_narrative_call() -> None:
    h = _harness(llm=ExactAdapter(text=VALID), state=ended_state())
    h.cancellation.cancel_requested = True
    assert narrate(h).refusal is RefusalCode.CANCELLED
    assert h.llm.prompts == []


def test_a_late_answer_after_cancellation_is_discarded() -> None:
    h = _harness(llm=ExactAdapter(text=VALID), state=ended_state())
    h.llm.during = lambda: setattr(h.cancellation, "cancel_requested", True)
    result = narrate(h)
    assert result.refusal is RefusalCode.CANCELLED and result.narrative is None


@pytest.mark.parametrize(
    "text",
    [
        "I can't help with that.",
        '{"sections":[{"name":"findings","claims":[{"type":"prose","text":"Churn doubled."}]}]}',
        '{"sections":[{"name":"findings","claims":[],"note":"extra"}]}',
        '{"sections":[]}',
        "[]",
    ],
)
def test_malformed_output_or_a_refusal_is_invalid_and_never_retried(text: str) -> None:
    h = _harness(llm=ExactAdapter(text=text), state=ended_state())
    result = narrate(h)
    assert result.refusal is None and result.review_status == "invalid"
    assert result.narrative is None and len(h.llm.prompts) == 1


def test_a_provider_failure_is_unavailable() -> None:
    h = _harness(
        llm=ExactAdapter(error=LLMGenerationError("provider exploded")), state=ended_state()
    )
    result = narrate(h)
    assert result.refusal is None and result.review_status == "unavailable"
    assert restore_state(h.state).decisions[-1].category == "provider_failure"


def test_an_over_large_prompt_never_runs() -> None:
    h = _harness(llm=ExactAdapter(text=VALID), state=ended_state(), max_input=200)
    result = narrate(h)
    assert result.refusal is None and result.review_status == "unavailable"
    assert h.llm.prompts == []


def test_readiness_is_checked_without_a_decision_or_charge() -> None:
    h = _harness(llm=ExactAdapter(text=VALID), state=ended_state())
    before = len(restore_state(h.state).decisions)
    assert h.gateway.narrative_readiness() == "ready"
    assert h.ledger.usage().model_calls == 0 and len(restore_state(h.state).decisions) == before
    exhausted = _harness(
        llm=ExactAdapter(text=VALID),
        state=ended_state(),
        limits=BudgetLimits(**{**EXECUTION_LIMITS.model_dump(), "model_calls": 0}),
    )
    assert exhausted.gateway.narrative_readiness() == "budget_unavailable"
    assert exhausted.ledger.closed_reason is None  # a check never closes the ledger
    unbounded = _harness(llm=_gemini(), state=ended_state())
    assert unbounded.gateway.narrative_readiness() == "model_unavailable"
    h.gateway.close("completed")
    assert h.gateway.narrative_readiness() == "closed"


def test_the_specialist_port_cannot_narrate() -> None:
    h = _harness(llm=ExactAdapter(text=VALID), state=ended_state())
    port = h.gateway.specialist_port()
    assert not hasattr(port, "compose_narrative") and not hasattr(port, "narrative_readiness")
    # A request type from another operation is refused before anything runs.
    result = h.gateway.compose_narrative(
        h.state, DraftFindings(role=AgentRole.BUSINESS_INVESTIGATOR, round=0)
    )
    assert result.refusal is RefusalCode.INVALID_REQUEST and h.llm.prompts == []


def test_parse_narrative_is_strict() -> None:
    assert parse_narrative(VALID) is not None
    for text in (None, 7, "", "x" * 40_000, "{", '{"sections": "x"}', '"sections"'):
        assert parse_narrative(text) is None


def test_the_rendered_prompt_is_deterministic() -> None:
    from packages.investigation.lifecycle import InvestigationStatus
    from packages.investigation.report import compose_body, report_inputs

    view = restore_state(ended_state())
    inputs = report_inputs(view, {}, status=InvestigationStatus.COMPLETED_PARTIAL)
    assert render_narrative_prompt(compose_body(inputs)) == render_narrative_prompt(
        compose_body(inputs)
    )
