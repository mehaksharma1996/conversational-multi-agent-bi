"""INV-0 (#133): every plan and follow-up validation rule, positive and negative."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields

import pytest

from packages.investigation import (
    EXECUTION_LIMITS,
    AgentRole,
    InvestigationPlan,
    PlanTask,
    PlanViolation,
    ResourceContext,
    SchemaContext,
    validate_follow_ups,
    validate_plan,
)
from packages.investigation.validation import MAX_DEPENDENCY_DEPTH
from tests.investigation_fixtures import (
    CHURN_SCHEMA,
    churn_plan,
    document_task,
    hypothesis_task,
    plan_of,
    sql_task,
)

FULL = ResourceContext(has_table=True, has_documents=True)


def _plan_codes(
    plan: InvestigationPlan,
    *,
    resources: ResourceContext = FULL,
    schema: SchemaContext = CHURN_SCHEMA,
    limits=EXECUTION_LIMITS,
) -> set[tuple[str, str | None]]:
    violations = validate_plan(plan, resources=resources, schema=schema, limits=limits)
    return {(v.code, v.task_id) for v in violations}


def test_churn_plan_is_valid() -> None:
    assert (
        validate_plan(churn_plan(), resources=FULL, schema=CHURN_SCHEMA, limits=EXECUTION_LIMITS)
        == ()
    )


PLAN_CASES: list[tuple[str, Callable[[], InvestigationPlan], tuple[str, str | None]]] = [
    (
        "duplicate_task_id",
        lambda: plan_of(sql_task("trend"), document_task("trend")),
        ("duplicate_task_id", "trend"),
    ),
    (
        "unknown_dependency",
        lambda: plan_of(sql_task("segments", depends_on=("missing",))),
        ("unknown_dependency", "segments"),
    ),
    (
        "self_dependency",
        lambda: plan_of(sql_task("trend", depends_on=("trend",))),
        ("self_dependency", "trend"),
    ),
    (
        "dependency_cycle",
        lambda: plan_of(sql_task("a", depends_on=("b",)), sql_task("b", depends_on=("a",))),
        ("dependency_cycle", "a"),
    ),
    (
        "dependency_too_deep",
        lambda: plan_of(
            *(
                sql_task(f"t{i}", depends_on=((f"t{i - 1}",) if i else ()))
                for i in range(MAX_DEPENDENCY_DEPTH + 1)
            )
        ),
        ("dependency_too_deep", f"t{MAX_DEPENDENCY_DEPTH}"),
    ),
    (
        "proposer_not_allowed",
        # The Supervisor plans; hypothesis tests come only from the Business agent or the Critic.
        lambda: plan_of(hypothesis_task("h1-test")),
        ("proposer_not_allowed", "h1-test"),
    ),
    (
        "assignee_not_allowed",
        lambda: plan_of(sql_task("trend", assignee=AgentRole.CRITIC)),
        ("assignee_not_allowed", "trend"),
    ),
    (
        "unknown_field",
        lambda: plan_of(sql_task("trend", fields=("quarter", "lifetime_value"))),
        ("unknown_field", "trend"),
    ),
    (
        "unknown_evidence",
        # No evidence exists while planning, so a plan cannot cite any.
        lambda: plan_of(sql_task("trend", evidence_refs=("ev_1",))),
        ("unknown_evidence", "trend"),
    ),
]


@pytest.mark.parametrize(
    ("build", "expected"), [case[1:] for case in PLAN_CASES], ids=[c[0] for c in PLAN_CASES]
)
def test_plan_rule_rejects(
    build: Callable[[], InvestigationPlan], expected: tuple[str, str | None]
) -> None:
    assert expected in _plan_codes(build())


def test_cycle_reports_every_task_in_the_cycle() -> None:
    plan = plan_of(sql_task("a", depends_on=("b",)), sql_task("b", depends_on=("a",)))
    assert _plan_codes(plan) == {("dependency_cycle", "a"), ("dependency_cycle", "b")}


def test_chain_at_the_depth_limit_is_allowed() -> None:
    chain = [
        sql_task(f"t{i}", depends_on=((f"t{i - 1}",) if i else ()))
        for i in range(MAX_DEPENDENCY_DEPTH)
    ]
    assert _plan_codes(plan_of(*chain)) == set()


@pytest.mark.parametrize(
    ("resources", "task", "allowed"),
    [
        (ResourceContext(has_table=True, has_documents=False), document_task("pricing"), False),
        (ResourceContext(has_table=False, has_documents=True), sql_task("trend"), False),
        (ResourceContext(has_table=True, has_documents=False), sql_task("trend"), True),
        (ResourceContext(has_table=False, has_documents=True), document_task("pricing"), True),
    ],
    ids=["documents-missing", "table-missing", "table-present", "documents-present"],
)
def test_resource_rule(resources: ResourceContext, task: PlanTask, allowed: bool) -> None:
    codes = _plan_codes(plan_of(task), resources=resources)
    assert (("resource_unavailable", task.id) not in codes) is allowed


def test_schema_fields_match_case_insensitively() -> None:
    assert _plan_codes(plan_of(sql_task("trend", fields=("Quarter", "CHURNED")))) == set()


def test_plan_must_fit_the_task_budget() -> None:
    limits = EXECUTION_LIMITS.model_copy(update={"task_attempts": 3})
    assert ("task_budget_exceeded", None) in _plan_codes(churn_plan(), limits=limits)
    exact = EXECUTION_LIMITS.model_copy(update={"task_attempts": len(churn_plan().tasks)})
    assert _plan_codes(churn_plan(), limits=exact) == set()


def test_violations_are_ordered_and_content_free() -> None:
    plan = plan_of(
        sql_task("zeta", fields=("secret_column",)),
        sql_task("alpha", assignee=AgentRole.CRITIC, depends_on=("missing",)),
    )
    violations = validate_plan(plan, resources=FULL, schema=CHURN_SCHEMA, limits=EXECUTION_LIMITS)
    assert violations == tuple(sorted(violations, key=lambda v: (v.task_id or "", v.code)))
    assert {field.name for field in fields(PlanViolation)} == {"code", "task_id"}
    assert "secret_column" not in repr(violations)


# --- follow-up rounds ------------------------------------------------------------------------

KNOWN_EVIDENCE = frozenset({"ev_1", "ev_2"})
KNOWN_HYPOTHESES = frozenset({"h1", "h2"})


def _follow_up_codes(
    requests: list[PlanTask],
    *,
    proposer: AgentRole = AgentRole.CRITIC,
    admitted: tuple[PlanTask, ...] = (),
    completed_rounds: int = 0,
    resources: ResourceContext = FULL,
    limits=EXECUTION_LIMITS,
) -> set[tuple[str, str | None]]:
    violations = validate_follow_ups(
        requests,
        proposer=proposer,
        plan=churn_plan(),
        admitted=admitted,
        completed_rounds=completed_rounds,
        evidence_ids=KNOWN_EVIDENCE,
        hypothesis_ids=KNOWN_HYPOTHESES,
        resources=resources,
        schema=CHURN_SCHEMA,
        limits=limits,
    )
    return {(v.code, v.task_id) for v in violations}


def _valid_round() -> list[PlanTask]:
    return [
        sql_task(
            "tenure-split",
            depends_on=("segments",),
            fields=("plan_tier", "tenure_months", "churned"),
            evidence_refs=("ev_1",),
        ),
        hypothesis_task("h1-tenure", depends_on=("segments",)),
        hypothesis_task(
            "h2-docs",
            hypothesis_id="h2",
            method="document_check",
            fields=(),
            query="APAC support migration",
            assignee=AgentRole.DOCUMENT_RESEARCH,
        ),
    ]


def test_valid_critic_round_is_admitted() -> None:
    assert _follow_up_codes(_valid_round()) == set()


def test_business_investigator_may_propose_hypothesis_tests() -> None:
    requests = [hypothesis_task("h1-tenure", depends_on=("segments",))]
    assert _follow_up_codes(requests, proposer=AgentRole.BUSINESS_INVESTIGATOR) == set()


def test_last_allowed_round_is_admitted() -> None:
    last = EXECUTION_LIMITS.replan_rounds - 1
    assert _follow_up_codes(_valid_round(), completed_rounds=last) == set()


FOLLOW_UP_CASES: list[tuple[str, dict[str, object], tuple[str, str | None]]] = [
    (
        "follow-up-proposer",
        {"proposer": AgentRole.SUPERVISOR},
        ("proposer_not_allowed", None),
    ),
    (
        "kind-proposer",
        {"proposer": AgentRole.BUSINESS_INVESTIGATOR},
        ("proposer_not_allowed", "tenure-split"),
    ),
    (
        "round-limit",
        {"completed_rounds": EXECUTION_LIMITS.replan_rounds},
        ("round_limit_exceeded", None),
    ),
    (
        "task-budget",
        {"limits": EXECUTION_LIMITS.model_copy(update={"task_attempts": 6})},
        ("task_budget_exceeded", None),
    ),
]


@pytest.mark.parametrize(
    ("options", "expected"),
    [case[1:] for case in FOLLOW_UP_CASES],
    ids=[c[0] for c in FOLLOW_UP_CASES],
)
def test_follow_up_round_rule_rejects(
    options: dict[str, object], expected: tuple[str, str | None]
) -> None:
    assert expected in _follow_up_codes(_valid_round(), **options)  # type: ignore[arg-type]


def test_too_many_follow_ups_in_one_round() -> None:
    requests = [*_valid_round(), sql_task("extra", fields=("region", "churned"))]
    assert ("too_many_follow_ups", None) in _follow_up_codes(requests)


def test_follow_up_cannot_reuse_a_plan_task_id() -> None:
    assert ("duplicate_task_id", "trend") in _follow_up_codes([sql_task("trend")])


def test_follow_up_cannot_close_a_cycle_with_earlier_rounds() -> None:
    admitted = (sql_task("earlier", depends_on=("later",)),)
    codes = _follow_up_codes([sql_task("later", depends_on=("earlier",))], admitted=admitted)
    assert ("dependency_cycle", "later") in codes


def test_follow_up_may_depend_on_admitted_tasks() -> None:
    admitted = (sql_task("earlier", depends_on=("segments",)),)
    requests = [sql_task("later", depends_on=("earlier",))]
    assert _follow_up_codes(requests, admitted=admitted) == set()


def test_follow_up_references_must_exist() -> None:
    codes = _follow_up_codes(
        [
            sql_task("tenure-split", evidence_refs=("ev_9",)),
            hypothesis_task("h7-test", hypothesis_id="h7"),
        ]
    )
    assert ("unknown_evidence", "tenure-split") in codes
    assert ("unknown_hypothesis", "h7-test") in codes


def test_hypothesis_method_must_match_assignee() -> None:
    codes = _follow_up_codes(
        [
            hypothesis_task("h1-sql", assignee=AgentRole.DOCUMENT_RESEARCH),
            hypothesis_task(
                "h2-docs",
                hypothesis_id="h2",
                method="document_check",
                fields=(),
                query="APAC support migration",
                assignee=AgentRole.SQL_ANALYST,
            ),
        ]
    )
    assert ("method_assignee_mismatch", "h1-sql") in codes
    assert ("method_assignee_mismatch", "h2-docs") in codes


def test_document_check_needs_documents() -> None:
    requests = [_valid_round()[2]]
    codes = _follow_up_codes(
        requests, resources=ResourceContext(has_table=True, has_documents=False)
    )
    assert ("resource_unavailable", "h2-docs") in codes


def test_negative_round_count_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        _follow_up_codes(_valid_round(), completed_rounds=-1)
