"""INV-7 S2 (#140): the Supervisor builds follow-ups deterministically; validation bounds them."""

from __future__ import annotations

from typing import Any

import pytest

from packages.investigation.budget import EXECUTION_LIMITS
from packages.investigation.hypotheses import FollowUpTask, Stratification, resolve_follow_up
from packages.investigation.replanning import (
    FOLLOW_UP_CRITERION,
    FollowUpApprovalRequest,
    FollowUpBuildError,
    build_follow_ups,
)
from packages.investigation.state import CritiqueFinding, InvestigationScope, follow_up_digest
from packages.investigation.validation import ResourceContext, validate_follow_up_round
from tests.investigation_churn_schema import _TYPES, CHURN_SCHEMA, churn_v2_plan
from tests.investigation_critique_fakes import TENURE, gap_finding, stratified_follow_up
from tests.investigation_fixtures import SCOPE, sql_task

NUMERIC = frozenset(name for name, kind in _TYPES.items() if kind in {"integer", "number"})
FULL = ResourceContext(has_table=True, has_documents=True)


def _codes(requests: list[Any], **kwargs: Any) -> set[tuple[str, str | None]]:
    options: dict[str, Any] = {
        "plan": churn_v2_plan(),
        "admitted": (),
        "completed_rounds": 0,
        "evidence_ids": frozenset(),
        "resources": FULL,
        "schema": CHURN_SCHEMA,
        "numeric_fields": NUMERIC,
        "limits": EXECUTION_LIMITS,
    }
    options.update(kwargs)
    return {(v.code, v.task_id) for v in validate_follow_up_round(requests, **options)}


def test_a_confirmed_gap_becomes_an_exact_stratified_copy_of_its_target() -> None:
    plan = churn_v2_plan()
    findings = (
        CritiqueFinding(
            code="causal_overreach",
            severity="high",
            disposition="downgrade_to_association",
            claim_index=0,
        ),
        gap_finding(),
    )
    tasks, indexes = build_follow_ups(plan, findings, round_index=1)
    assert indexes == (1,)
    (task,) = tasks
    assert type(task) is FollowUpTask
    assert (task.id, task.depends_on, task.max_attempts) == ("h1_rate_r1s0", ("h1_rate",), 1)
    assert task.success_criterion == FOLLOW_UP_CRITERION
    resolved = resolve_follow_up(task)
    from packages.investigation.hypotheses import resolve_test

    target = resolve_test(plan, next(t for t in plan.tasks if t.id == "h1_rate"))
    for name in ("hypothesis_id", "metric", "filters", "period", "claim"):
        assert getattr(resolved, name) == getattr(target, name)
    assert resolved.stratification == TENURE
    assert _codes(list(tasks)) == set()


def test_building_is_deterministic() -> None:
    plan = churn_v2_plan()
    first = build_follow_ups(plan, (gap_finding(),), round_index=2)
    assert first == build_follow_ups(plan, (gap_finding(),), round_index=2)
    assert first[0][0].id == "h1_rate_r2s0"


def test_a_finding_that_names_no_approved_test_cannot_be_built() -> None:
    plan = churn_v2_plan()
    with pytest.raises(FollowUpBuildError):
        build_follow_ups(plan, (gap_finding(task="trend"),), round_index=1)
    with pytest.raises(FollowUpBuildError):
        build_follow_ups(plan, (gap_finding(task="ghost"),), round_index=1)


def test_a_follow_up_that_changes_its_target_is_refused() -> None:
    widened = stratified_follow_up(filters=[{"field": "customer_segment", "value": "SMB"}])
    assert ("follow_up_target_mismatch", "h1_tenure") in _codes([widened])
    raw = stratified_follow_up().model_dump(mode="json")
    raw["input"]["claim"]["min_effect"] = 0.0001  # a lowered threshold
    assert ("follow_up_target_mismatch", "h1_tenure") in _codes([FollowUpTask.model_validate(raw)])
    raw = stratified_follow_up().model_dump(mode="json")
    raw["input"]["hypothesis_id"] = "h2"
    assert ("follow_up_target_mismatch", "h1_tenure") in _codes([FollowUpTask.model_validate(raw)])


def test_only_stratified_re_tests_are_allowed() -> None:
    assert ("follow_up_kind_not_allowed", "drill") in _codes([sql_task("drill")])


def test_the_stratification_field_must_be_a_confirmed_numeric_field() -> None:
    strata = Stratification(field="region", cut_points=(1,))
    assert ("unknown_field", "h1_tenure") in _codes([stratified_follow_up(stratification=strata)])
    strata = Stratification(field="secret_column", cut_points=(1,))
    assert ("unknown_field", "h1_tenure") in _codes([stratified_follow_up(stratification=strata)])


def test_round_task_and_budget_limits_apply() -> None:
    task = stratified_follow_up()
    assert ("round_limit_exceeded", None) in _codes([task], completed_rounds=2)
    many = [stratified_follow_up(f"t{i}") for i in range(4)]
    assert ("too_many_follow_ups", None) in _codes(many)
    tight = EXECUTION_LIMITS.model_copy(update={"task_attempts": 8})
    assert ("task_budget_exceeded", None) in _codes([task], limits=tight)


def test_ids_and_dependencies_are_checked_against_plan_and_earlier_rounds() -> None:
    clash = stratified_follow_up("h1_rate")
    assert ("duplicate_task_id", "h1_rate") in _codes([clash])
    earlier = stratified_follow_up()
    assert ("duplicate_task_id", "h1_tenure") in _codes([earlier], admitted=(earlier,))
    ghost = stratified_follow_up("h1_tenure", target="ghost")
    codes = _codes([ghost])
    assert ("unknown_dependency", "h1_tenure") in codes
    assert ("follow_up_target_mismatch", "h1_tenure") in codes


def test_an_approval_request_binds_the_exact_task_set() -> None:
    tasks = (stratified_follow_up(),)
    request = FollowUpApprovalRequest.of(
        InvestigationScope.model_validate(SCOPE), "a" * 64, 1, tasks
    )
    assert request.follow_up_digest == follow_up_digest(tasks)
    assert request.round == 1 and request.tasks == tasks
