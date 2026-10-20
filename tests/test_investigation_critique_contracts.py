"""INV-7 S1 (#140): critique, follow-up, and standardized-evidence contracts and state rules."""

from __future__ import annotations

from datetime import timedelta
from fractions import Fraction
from typing import Any

import pytest
from pydantic import ValidationError

from packages.investigation.evidence import (
    DerivedEvidencePayload,
    DerivedRatio,
    DerivedStratum,
    crude_baseline,
    exact_ratio,
    standardized_rate,
)
from packages.investigation.hypotheses import (
    FollowUpTask,
    Stratification,
    StratifiedTestSpec,
    parse_follow_up,
    resolve_follow_up,
)
from packages.investigation.refusals import SEVERITY, RefusalCode, RefusalSeverity
from packages.investigation.state import (
    FINDING_RULES,
    CritiqueFinding,
    CritiqueRecord,
    FollowUpApproval,
    FollowUpRoundRecord,
    StateUpdateRejected,
    follow_up_digest,
    owned_update,
    restore_state,
)
from tests.investigation_critique_fakes import (
    TENURE,
    admission,
    admitted_state,
    approval_for,
    critique,
    gap_finding,
    round_record,
    stratified_follow_up,
    v2_running_state,
)
from tests.investigation_fixtures import apply_update, evidence_record, running_state, task_record

EV = "ev_" + "1" * 32


def _rejected(code: str, owner: Any, state: dict[str, Any], update: dict[str, Any]) -> None:
    with pytest.raises(StateUpdateRejected) as caught:
        owned_update(owner, state, update)
    assert caught.value.code == code


def _spec(**changes: Any) -> dict[str, Any]:
    raw = stratified_follow_up().input.model_dump(mode="json")
    raw.update(changes)
    return raw


# --- stratification and the follow-up spec -----------------------------------------------------


def test_stratification_bands_are_fixed_and_cut_points_ascend() -> None:
    assert TENURE.bands == ("band_0", "band_1")
    assert Stratification(field="x", cut_points=(1, 5, 9)).bands == (
        "band_0",
        "band_1",
        "band_2",
        "band_3",
    )
    for cuts in ((), (6, 6), (9, 3), (1, 2, 3, 4), (True,), (6.0,), (10**7,)):
        with pytest.raises(ValidationError):
            Stratification.model_validate({"field": "x", "cut_points": list(cuts)})


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "trend_comparison"},
        {"metric": {"type": "mean", "value": "avg_resolution_hours"}},
        {"metric": None},
        {"period": None},
        {"claim": {"direction": "higher_than", "min_effect": 0.01}},
        {"stratification": {"field": "plan_tier", "cut_points": [1]}},  # already a filter key
        {"stratification": {"field": "churned", "cut_points": [1]}},  # a count field
        {"stratification": {"field": "QUARTER_START", "cut_points": [1]}},  # the period, any case
        {"query": "free text"},
        {"unexpected": 1},
    ],
)
def test_a_follow_up_spec_is_an_explicit_stratified_rate_re_test(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StratifiedTestSpec.model_validate(_spec(**changes))


def test_a_follow_up_task_runs_once_through_the_sql_analyst_after_its_target() -> None:
    raw = stratified_follow_up().model_dump(mode="json")
    cases: tuple[dict[str, Any], ...] = (
        {"assignee": "document_research"},
        {"assignee": "critic"},
        {"max_attempts": 2},
        {"depends_on": []},
        {"depends_on": ["h1_rate", "trend"]},
        {"kind": "sql_analysis"},
    )
    for changes in cases:
        with pytest.raises(ValidationError):
            FollowUpTask.model_validate({**raw, **changes})


def test_follow_ups_dispatch_exactly_on_the_stratified_shape() -> None:
    task = stratified_follow_up()
    assert type(parse_follow_up(task.model_dump(mode="json"))) is FollowUpTask
    raw = task.model_dump(mode="json")
    raw["input"].pop("stratification")
    with pytest.raises(ValidationError):  # without strata it is a v1 task, which forbids the spec
        parse_follow_up(raw)


def test_resolving_a_follow_up_uses_its_own_explicit_spec() -> None:
    resolved = resolve_follow_up(stratified_follow_up())
    assert resolved.stratification == TENURE
    assert resolved.method == "stratified_comparison"
    assert "tenure_months_start" in resolved.fields
    assert resolved.comparison_group is None


# --- standardized derived evidence (decision C4) -----------------------------------------------


def _stratum(label: str, base: tuple[int, int], comparison: tuple[int, int]) -> DerivedStratum:
    return DerivedStratum(
        label=label,
        baseline=DerivedRatio(numerator=float(base[0]), denominator=base[1]),
        comparison=DerivedRatio(numerator=float(comparison[0]), denominator=comparison[1]),
    )


# Arbitrary counts, not the churn fixture: the arithmetic is checked by hand below.
STRATA = (_stratum("band_0", (10, 40), (12, 30)), _stratum("band_1", (5, 60), (9, 50)))


def _standardized(**changes: Any) -> DerivedEvidencePayload:
    standard = exact_ratio(standardized_rate(STRATA))
    crude = crude_baseline(STRATA)
    raw: dict[str, Any] = {
        "evidence_id": "ev_" + "2" * 32,
        "task_id": "h1_tenure",
        "attempt": 1,
        "operation": "standardized_rate_difference",
        "source_evidence_ids": [EV],
        "measure": standard.model_dump(),
        "reference": crude.model_dump(),
        "value": float(standard.exact - crude.exact),
        "strata": [item.model_dump() for item in STRATA],
        "standardized": "measure",
    }
    raw.update(changes)
    return DerivedEvidencePayload.model_validate(raw)


def test_standardization_uses_baseline_weights_exactly() -> None:
    # weights 40/100 and 60/100; comparison rates 12/30 and 9/50
    expected = Fraction(40, 100) * Fraction(12, 30) + Fraction(60, 100) * Fraction(9, 50)
    assert standardized_rate(STRATA) == expected
    assert crude_baseline(STRATA).exact == Fraction(15, 100)
    payload = _standardized()
    assert payload.measure.exact == expected
    assert payload.value == float(expected - Fraction(15, 100))


@pytest.mark.parametrize(
    "changes",
    [
        {"value": 0.5},
        {"measure": {"numerator": 1.0, "denominator": 4}},
        {"reference": {"numerator": 16.0, "denominator": 100}},
        {"standardized": None},
        {"standardized": "reference"},  # the sides would then disagree with their contents
        {"strata": []},
        {"strata": [STRATA[0].model_dump()]},  # one band is not a stratification
        {"operation": "rate_difference"},
    ],
)
def test_a_standardized_payload_with_any_altered_part_cannot_be_built(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        _standardized(**changes)


def test_altered_band_counts_or_labels_cannot_be_built() -> None:
    altered = [item.model_dump() for item in STRATA]
    altered[1]["comparison"]["numerator"] = 10.0
    with pytest.raises(ValidationError):
        _standardized(strata=altered)
    swapped = [STRATA[1].model_dump(), STRATA[0].model_dump()]
    with pytest.raises(ValidationError):
        _standardized(strata=swapped)
    overfull = [item.model_dump() for item in STRATA]
    overfull[0]["comparison"]["numerator"] = 31.0
    with pytest.raises(ValidationError):
        _standardized(strata=overfull)


def test_a_decrease_puts_the_standardized_rate_on_the_reference_side() -> None:
    standard = exact_ratio(standardized_rate(STRATA))
    crude = crude_baseline(STRATA)
    payload = _standardized(
        measure=crude.model_dump(),
        reference=standard.model_dump(),
        value=float(crude.exact - standard.exact),
        standardized="reference",
    )
    assert payload.reference.exact == standardized_rate(STRATA)


def test_other_operations_carry_no_strata() -> None:
    with pytest.raises(ValidationError):
        DerivedEvidencePayload.model_validate(
            {
                "evidence_id": "ev_" + "2" * 32,
                "task_id": "h1_rate",
                "attempt": 1,
                "operation": "rate_difference",
                "source_evidence_ids": [EV],
                "measure": {"numerator": 2.0, "denominator": 4},
                "reference": {"numerator": 1.0, "denominator": 4},
                "value": 0.25,
                "strata": [item.model_dump() for item in STRATA],
            }
        )


def test_an_inexact_standardized_rate_is_refused() -> None:
    with pytest.raises(ValueError):
        exact_ratio(Fraction(2**60 + 1, 2**61))


# --- critique records -------------------------------------------------------------------------


def test_severity_and_disposition_are_fixed_by_the_code() -> None:
    assert FINDING_RULES["missing_stratification"] == ("medium", "follow_up")
    with pytest.raises(ValidationError):
        CritiqueFinding(
            code="causal_overreach", severity="low", disposition="follow_up", claim_index=0
        )
    with pytest.raises(ValidationError):  # a deterministic finding never carries strata
        CritiqueFinding(
            code="numeric_mismatch",
            severity="high",
            disposition="correct_from_evidence",
            claim_index=0,
            stratification=TENURE,
        )
    with pytest.raises(ValidationError):  # a gap must be confirmed and name its target
        CritiqueFinding(
            code="missing_stratification",
            severity="medium",
            disposition="follow_up",
            stratification=TENURE,
        )
    with pytest.raises(ValidationError):  # claim findings name their claim, by index only
        CritiqueFinding(
            code="numeric_mismatch", severity="high", disposition="correct_from_evidence"
        )


def test_a_critique_record_is_content_free_and_its_replan_follows_its_findings() -> None:
    schema = CritiqueRecord.model_json_schema()
    text = str(schema)
    for word in ("statement", "value", "prompt", "text", "answer", "rows"):
        assert f"'{word}'" not in text
    with pytest.raises(ValidationError):
        CritiqueRecord(round=0, draft_status="valid", review_status="valid", replan="admitted")
    with pytest.raises(ValidationError):
        CritiqueRecord(
            round=0,
            draft_status="valid",
            review_status="valid",
            findings=(gap_finding(),),
            replan="none_needed",
        )
    with pytest.raises(ValidationError):  # only two critic rounds exist
        CritiqueRecord(round=2, draft_status="valid", review_status="valid", replan="none_needed")


def test_a_round_record_binds_its_approval_to_its_exact_task_set() -> None:
    tasks = [stratified_follow_up()]
    good = round_record(tasks)
    FollowUpRoundRecord.model_validate(good)
    other = [
        stratified_follow_up(
            stratification=Stratification(field="tenure_months_start", cut_points=(12,))
        )
    ]
    with pytest.raises(ValidationError):
        FollowUpRoundRecord.model_validate(
            {**good, "approval": approval_for(other, 1).model_dump(mode="json")}
        )
    with pytest.raises(ValidationError):
        FollowUpRoundRecord.model_validate(
            {**good, "approval": approval_for(tasks, 2).model_dump(mode="json")}
        )
    with pytest.raises(ValidationError):  # no third round
        FollowUpRoundRecord.model_validate({**good, "round": 3, "critique_round": 2})
    with pytest.raises(ValidationError):
        FollowUpApproval.model_validate(
            {**good["approval"], "deadline": good["approval"]["approved_at"]}
        )
    assert follow_up_digest(tasks) != follow_up_digest(other)


def test_the_follow_up_refusal_is_task_severity() -> None:
    assert SEVERITY[RefusalCode.FOLLOW_UP_NOT_AUTHORIZED] is RefusalSeverity.TASK


# --- state rules --------------------------------------------------------------------------------


def test_a_round_is_admitted_after_an_admitting_critique_and_restores() -> None:
    state = admitted_state()
    view = restore_state(state)
    assert [task.id for task in view.admitted_follow_ups] == ["h1_tenure"]
    assert type(view.admitted_follow_ups[0]) is FollowUpTask
    assert view.follow_up_rounds[0].round == 1


def test_follow_ups_without_an_approved_round_are_refused() -> None:
    state = apply_update(v2_running_state(), "critic", {"critiques": [critique(0)]})
    task = stratified_follow_up().model_dump(mode="json")
    _rejected("follow_up_unbound", "supervisor", state, {"admitted_follow_ups": [task]})


def test_a_round_needs_an_admitting_critique_first() -> None:
    _rejected(
        "follow_up_unbound", "supervisor", v2_running_state(), admission([stratified_follow_up()])
    )
    declined = apply_update(
        v2_running_state(), "critic", {"critiques": [critique(0, replan="not_authorized")]}
    )
    _rejected("follow_up_unbound", "supervisor", declined, admission([stratified_follow_up()]))


@pytest.mark.parametrize(
    "case",
    ["digest", "window_early", "window_late", "finding", "target", "strata"],
)
def test_a_round_must_match_its_approval_findings_and_targets(case: str) -> None:
    task = stratified_follow_up()
    state = apply_update(v2_running_state(), "critic", {"critiques": [critique(0)]})
    update = admission([task])
    record = update["follow_up_rounds"][0]
    if case == "digest":
        other = stratified_follow_up(
            stratification=Stratification(field="tenure_months_start", cut_points=(12,))
        )
        update["admitted_follow_ups"] = [other.model_dump(mode="json")]
    elif case == "window_early":
        approval = approval_for([task], 1)
        early = approval.model_copy(
            update={
                "approved_at": approval.approved_at - timedelta(days=2),
                "deadline": approval.approved_at - timedelta(days=1),
            }
        )
        record["approval"] = early.model_dump(mode="json")
    elif case == "window_late":
        record["approval"] = approval_for([task], 1, hours=48).model_dump(mode="json")
    elif case == "finding":
        record["finding_indexes"] = [5]
    elif case == "target":
        stray = stratified_follow_up(target="h4_rate")  # the finding names h1_rate
        update = admission([stray])
    elif case == "strata":
        stray = stratified_follow_up(
            stratification=Stratification(field="tenure_months_start", cut_points=(3,))
        )
        update = admission([stray])
    _rejected("follow_up_unbound", "supervisor", state, update)


def test_a_third_round_and_a_second_round_without_critique_one_are_impossible() -> None:
    state = admitted_state()
    second = [stratified_follow_up("h4_tenure", target="h4_rate", hypothesis="h4", filters=[])]
    # Round 2 follows critique 1, which does not exist yet.
    _rejected("follow_up_unbound", "supervisor", state, admission(second, 2))
    with pytest.raises(ValidationError):
        round_record(second, 3)


def test_critique_one_reviews_round_one_and_rounds_are_sequential() -> None:
    _rejected("critique_mismatch", "critic", v2_running_state(), {"critiques": [critique(1)]})
    state = admitted_state()
    _rejected("critique_mismatch", "critic", state, {"critiques": [critique(0)]})
    later = apply_update(state, "critic", {"critiques": [critique(1, replan="none_needed")]})
    assert len(restore_state(later).critiques) == 2


def test_findings_name_existing_evidence_and_admitted_tasks_only() -> None:
    fabricated = CritiqueFinding(
        code="numeric_mismatch",
        severity="high",
        disposition="correct_from_evidence",
        claim_index=0,
        hypothesis_id="h1",
        evidence_ids=(EV,),
    )
    record = critique(0, findings=(fabricated,), replan="none_needed")
    _rejected("critique_mismatch", "critic", v2_running_state(), {"critiques": [record]})
    ghost = gap_finding(task="ghost")
    record = critique(0, findings=(ghost,), replan="not_authorized")
    _rejected("critique_mismatch", "critic", v2_running_state(), {"critiques": [record]})
    unknown = gap_finding(hypothesis="h9")
    record = critique(0, findings=(unknown,), replan="not_authorized")
    _rejected("critique_mismatch", "critic", v2_running_state(), {"critiques": [record]})


def test_existing_evidence_can_support_a_finding() -> None:
    state = v2_running_state(h1_rate=task_record("h1_rate", "running", 1))
    record = evidence_record("h1_rate")
    state = apply_update(state, "dispatcher", {"evidence": {record["id"]: record}})
    finding = CritiqueFinding(
        code="numeric_mismatch",
        severity="high",
        disposition="correct_from_evidence",
        claim_index=0,
        hypothesis_id="h1",
        task_id="h1_rate",
        evidence_ids=(record["id"],),
    )
    update = {"critiques": [critique(0, findings=(finding,), replan="none_needed")]}
    assert restore_state(apply_update(state, "critic", update)).critiques[0].findings == (finding,)


def test_the_critic_writes_only_critiques_and_the_supervisor_only_its_own_channels() -> None:
    state = v2_running_state()
    for channel, value in (
        ("tasks", {"h1_rate": task_record("h1_rate")}),
        ("admitted_follow_ups", []),
        ("budget_usage", {}),
        ("plan_approval", state["plan_approval"]),
        ("verdicts", {}),
        ("follow_up_rounds", []),
    ):
        _rejected("foreign_channel", "critic", state, {channel: value})
    _rejected("foreign_channel", "supervisor", state, {"critiques": [critique(0)]})
    _rejected("foreign_channel", "dispatcher", state, {"critiques": [critique(0)]})


@pytest.mark.parametrize("status", ["queued", "awaiting_sql_approval"])
def test_critiques_are_written_only_while_running(status: str) -> None:
    from tests.investigation_churn_schema import churn_v2_plan

    state = running_state(plan=churn_v2_plan(), status=status)
    _rejected("not_running", "critic", state, {"critiques": [critique(0, replan="none_needed")]})


def test_a_critique_needs_a_v2_plan() -> None:
    record = critique(0, replan="none_needed")
    _rejected("critique_mismatch", "critic", running_state(), {"critiques": [record]})


def test_pre_inv7_checkpoints_restore_unchanged() -> None:
    state = v2_running_state()
    assert "critiques" not in state and "follow_up_rounds" not in state
    view = restore_state(state)
    assert view.critiques == () and view.follow_up_rounds == ()
    assert restore_state({**state, "critiques": [], "follow_up_rounds": []}) == view
