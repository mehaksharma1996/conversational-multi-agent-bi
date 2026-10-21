"""INV-7 S4 (#140): bounded critique and replanning in the real execution graph.

Every run is an approved churn investigation on the real CSV and four-PDF index, through the real
execution owner, gateway, dispatcher, and specialists, under scripted bounded adapters. Reviews
and approvals are scripted; findings, follow-ups, and verdicts are not.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from packages.investigation import InvestigationStatus
from packages.investigation.budget import EXECUTION_LIMITS
from packages.investigation.state import restore_state
from tests.investigation_critique_fakes import (
    ScriptedAuthorizer,
    gap,
    review,
    v7_execution,
)
from tests.investigation_execution_fakes import Flag, reset_replay_registry
from tests.investigation_hypothesis_fakes import churn_index

S = InvestigationStatus
TENURE = gap()
TICKETS = gap(field="support_tickets", cut=1)


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    reset_replay_registry()


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return churn_index(tmp_path_factory.mktemp("inv7_replanning_index"))


def _run(index: Any, **kwargs: Any) -> tuple[Any, Any]:
    e = v7_execution(index, **kwargs)
    return e, e.run.run()


def _replans(result: Any) -> list[str]:
    return [record.replan for record in result.state.critiques]


# --- round limits ------------------------------------------------------------------------------


def test_a_repeated_request_cannot_extend_the_loop_past_one_round(index: Any) -> None:
    e, result = _run(index, reviews=[review(TENURE)], authorizer=ScriptedAuthorizer())
    view = result.state
    assert result.status is S.COMPLETED
    assert _replans(result) == ["admitted", "none_needed"]
    assert view.critiques[1].rejected_proposals == 1  # the same gap, already addressed
    assert [r.task_ids for r in view.follow_up_rounds] == [("h1_rate_r1s0",)]
    assert view.budget_usage.replan_rounds == 1 and view.budget_usage.critic_rounds == 2
    assert len(e.llm.review_prompts) == 2


def test_round_two_is_admitted_only_when_justified_and_authorized_and_no_third_exists(
    index: Any,
) -> None:
    reviews = [review(TENURE), review(TICKETS), review(gap(field="active_days_last_30", cut=10))]
    e, result = _run(index, reviews=reviews, authorizer=ScriptedAuthorizer())
    view = result.state
    assert _replans(result) == ["admitted", "admitted"]
    assert [r.round for r in view.follow_up_rounds] == [1, 2]
    assert [t.id for t in view.admitted_follow_ups] == ["h1_rate_r1s0", "h1_rate_r2s0"]
    usage = view.budget_usage
    assert (usage.critic_rounds, usage.replan_rounds) == (2, 2)
    assert len(e.llm.review_prompts) == 2  # no third review: critic rounds are spent
    assert view.verdicts["h1"].test_task_ids == ("h1_rate", "h1_rate_r1s0", "h1_rate_r2s0")
    assert result.status is S.COMPLETED


def test_the_round_limit_comes_from_the_budget(index: Any) -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"replan_rounds": 1})
    _, result = _run(
        index,
        reviews=[review(TENURE), review(TICKETS)],
        authorizer=ScriptedAuthorizer(),
        limits=limits,
    )
    assert _replans(result) == ["admitted", "round_limit"]
    assert len(result.state.follow_up_rounds) == 1


def test_one_critic_round_reviews_once_and_still_reassesses_the_follow_up(index: Any) -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"critic_rounds": 1})
    e, result = _run(
        index, reviews=[review(TENURE)], authorizer=ScriptedAuthorizer(), limits=limits
    )
    assert _replans(result) == ["admitted"]
    assert len(e.llm.review_prompts) == 1
    assert result.state.verdicts["h1"].test_task_ids == ("h1_rate", "h1_rate_r1s0")


def test_cumulative_task_attempts_can_end_replanning_early(index: Any) -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"task_attempts": 9})
    _, result = _run(
        index, reviews=[review(TENURE)], authorizer=ScriptedAuthorizer(), limits=limits
    )
    assert _replans(result) == ["budget"]
    assert result.state.admitted_follow_ups == ()
    assert result.status is S.COMPLETED


# --- authorization ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode", ["deny", "raise", "wrong_digest", "wrong_round", "expired", "outside_plan"]
)
def test_an_unauthorized_round_is_never_admitted(index: Any, mode: str) -> None:
    authorizer = ScriptedAuthorizer(mode)
    _, result = _run(index, reviews=[review(TENURE)], authorizer=authorizer)
    view = result.state
    assert _replans(result) == ["not_authorized"]
    assert view.admitted_follow_ups == () and view.follow_up_rounds == ()
    assert view.budget_usage.replan_rounds == 0
    assert len(authorizer.requests) == 1
    request = authorizer.requests[0]
    assert request.round == 1 and [t.id for t in request.tasks] == ["h1_rate_r1s0"]
    assert view.verdicts["h1"].test_task_ids == ("h1_rate",)  # verdicts still written


def test_without_an_authorizer_production_fails_closed(index: Any) -> None:
    _, result = _run(index, reviews=[review(TENURE)])
    assert _replans(result) == ["not_authorized"]
    assert result.state.admitted_follow_ups == ()
    assert result.status is S.COMPLETED  # the approved work is still reported


def test_a_follow_up_whose_approval_expires_before_it_runs_is_refused(index: Any) -> None:
    e = v7_execution(index, reviews=[review(TENURE)], authorizer=ScriptedAuthorizer())
    run = e.run
    original = run._register_follow_ups

    def register_late() -> Any:
        e.clock.now_value = e.clock.now_value + timedelta(hours=7)  # past the round approval
        return original()

    run._register_follow_ups = register_late
    result = run.run()
    view = result.state
    record = view.tasks["h1_rate_r1s0"]
    assert record.status == "refused" and record.outcome.error_category == "policy_refused"
    assert any(d.refusal_code == "follow_up_not_authorized" for d in view.decisions)
    assert view.verdicts["h1"].verdict == "untestable"  # never upgraded on missing evidence
    assert "test_not_completed" in view.verdicts["h1"].reasons
    assert result.status is S.COMPLETED_PARTIAL


# --- malformed and unconfirmed model output ----------------------------------------------------


def test_malformed_reviews_and_drafts_change_nothing_and_are_not_retried(index: Any) -> None:
    e, result = _run(
        index,
        drafts=["not json"],
        reviews=['{"gaps": "all of them"}'],
        authorizer=ScriptedAuthorizer(),
    )
    record = result.state.critiques[0]
    assert (record.draft_status, record.review_status, record.replan) == (
        "invalid",
        "invalid",
        "none_needed",
    )
    assert len(e.llm.draft_prompts) == 1 and len(e.llm.review_prompts) == 1
    assert result.status is S.COMPLETED


def test_unconfirmed_gaps_never_become_tasks(index: Any) -> None:
    bad = [gap(field="region"), gap(task="h2_resolution"), gap(field="secret_column")]
    _, result = _run(index, reviews=[review(*bad)], authorizer=ScriptedAuthorizer())
    record = result.state.critiques[0]
    assert record.rejected_proposals == 3 and record.replan == "none_needed"
    assert result.state.admitted_follow_ups == ()


# --- cancellation and budgets ------------------------------------------------------------------


def test_cancellation_during_review_discards_the_late_answer(index: Any) -> None:
    e = v7_execution(
        index, reviews=[review(TENURE)], authorizer=ScriptedAuthorizer(), cancellation=Flag()
    )
    e.llm.during = lambda: (
        setattr(e.cancellation, "cancel_requested", True) if e.llm.review_prompts else None
    )
    result = e.run.run()
    view = result.state
    assert result.status is S.CANCELLED
    assert view.critiques == () and view.verdicts == {}
    assert view.admitted_follow_ups == ()


def test_cancellation_during_replanning_admits_nothing(index: Any) -> None:
    flag = Flag()

    class CancellingAuthorizer(ScriptedAuthorizer):
        def authorize(self, request: Any) -> Any:
            approval = super().authorize(request)
            flag.cancel_requested = True
            return approval

    _, result = _run(
        index, reviews=[review(TENURE)], authorizer=CancellingAuthorizer(), cancellation=flag
    )
    view = result.state
    assert result.status is S.CANCELLED
    assert view.admitted_follow_ups == () and view.follow_up_rounds == ()
    assert view.budget_usage.replan_rounds == 0
    assert view.verdicts == {}


def test_budget_exhaustion_during_a_follow_up_stops_without_verdicts(index: Any) -> None:
    # 9 approved tasks call a model once each, plus the draft and review: the follow-up's SQL
    # generation is the 12th call.
    limits = EXECUTION_LIMITS.model_copy(update={"model_calls": 11})
    _, result = _run(
        index, reviews=[review(TENURE)], authorizer=ScriptedAuthorizer(), limits=limits
    )
    view = result.state
    assert result.status is S.BUDGET_EXHAUSTED
    assert [r.round for r in view.follow_up_rounds] == [1]
    assert view.verdicts == {}  # INV-6: no verdict after a stop
    assert view.critiques[0].verdicts  # round 0's assessment is retained
    assert view.budget_usage.model_calls <= 11


# --- state stays content-free and valid ---------------------------------------------------------


def test_every_checkpoint_restores_and_holds_no_claim_or_number(index: Any) -> None:
    import json

    canary = "CLAIM-CANARY-9093"
    draft = json.dumps(
        {
            "claims": [
                {
                    "hypothesis_id": "h1",
                    "test_task_id": "h1_rate",
                    "basis": "data",
                    "side": "comparison",
                    "value": 0.18,
                    "relationship": "causation",
                    "evidence_ids": ["ev_deadbeef"],
                    "statement": canary,
                }
            ]
        }
    )
    e = v7_execution(
        index, drafts=[draft], reviews=[review(TENURE)], authorizer=ScriptedAuthorizer()
    )
    result = e.run.run()
    assert result.status is S.COMPLETED
    restored = 0
    for snapshot in e.run.graph.get_state_history(e.run.config):
        if snapshot.values.get("status"):
            restore_state(snapshot.values)
            restored += 1
    assert restored > 20
    values = e.run.graph.get_state(e.run.config).values
    everything = json.dumps(values, default=str)
    assert canary not in everything and "ev_deadbeef" not in everything
    review_channels = json.dumps(
        {name: values[name] for name in ("critiques", "follow_up_rounds", "verdicts")}
    )
    for secret in ("0.18", "0.14", "0.081", "0.141", "causation", "statement", "SELECT"):
        assert secret not in review_channels
    codes = [finding["code"] for finding in values["critiques"][0]["findings"]]
    assert codes[:2] == ["causal_overreach", "unsupported_reference"]


# --- the interim report states what did not happen -----------------------------------------------


def _limitations(result: Any) -> set[str]:
    from packages.investigation.interim_report import compose_interim_report

    return {item.code for item in compose_interim_report(result.state, result.payloads).limitations}


def test_the_report_states_an_unauthorized_follow_up(index: Any) -> None:
    _, result = _run(index, reviews=[review(TENURE)])
    assert "follow_up_not_authorized" in _limitations(result)


def test_the_report_states_that_round_two_results_were_not_critiqued(index: Any) -> None:
    _, result = _run(
        index, reviews=[review(TENURE), review(TICKETS)], authorizer=ScriptedAuthorizer()
    )
    assert "critique_rounds_exhausted" in _limitations(result)


def test_the_report_states_an_incomplete_critique(index: Any) -> None:
    _, result = _run(index, reviews=["not json"], authorizer=ScriptedAuthorizer())
    assert "critique_unavailable" in _limitations(result)
    reset_replay_registry()
    _, limited = _run(
        index,
        reviews=[review(TENURE)],
        authorizer=ScriptedAuthorizer(),
        limits=EXECUTION_LIMITS.model_copy(update={"task_attempts": 9}),
    )
    assert "replan_limit_reached" in _limitations(limited)


def test_missing_source_documents_stay_missing(index: Any) -> None:
    import json

    claim = {
        "hypothesis_id": "h4",
        "test_task_id": "h4_rate",
        "basis": "document",
        "direction": "increase",
        "evidence_ids": ["ev_" + "0" * 32],  # the memo evidence that was never produced
    }
    _, result = _run(
        index,
        drafts=[json.dumps({"claims": [claim]})],
        reviews=[review(TENURE)],
        authorizer=ScriptedAuthorizer(),
        retriever=None,
    )
    view = result.state
    for task_id in ("pricing", "support", "playbook", "memo"):
        record = view.tasks[task_id]
        assert (record.status, record.outcome.error_category) == ("skipped", "resource_unavailable")
    finding = view.critiques[0].findings[0]
    assert (finding.code, finding.evidence_ids) == ("unsupported_reference", ())
    # H1 needs its pricing context: without the document it can never be supported.
    assert view.verdicts["h1"].verdict == "untestable"
    assert "context_unverified" in view.verdicts["h1"].reasons
    assert result.status is S.COMPLETED_PARTIAL
