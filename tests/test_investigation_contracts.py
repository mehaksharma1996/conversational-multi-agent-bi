"""INV-0 (#133): investigation contracts are versioned, closed, bounded, and JSON-safe."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError

from packages.investigation import (
    EXECUTION_LIMITS,
    SCHEMA_VERSION,
    TASK_EXECUTORS,
    TASK_PROPOSERS,
    AgentRole,
    BudgetUsage,
    CostEstimate,
    DocumentResearchInput,
    EvidenceRef,
    HypothesisTestInput,
    InvestigationPlan,
    PlanTask,
    ProviderCostProfile,
    SQLAnalysisInput,
    TaskKind,
    TaskOutcome,
)
from tests.investigation_fixtures import (
    churn_plan,
    document_task,
    hypothesis_task,
    plan_of,
    sql_task,
)


def test_architecture_has_all_five_agent_roles() -> None:
    assert {role.value for role in AgentRole} == {
        "supervisor",
        "sql_analyst",
        "document_research",
        "business_investigator",
        "critic",
    }


def test_every_task_kind_has_executors_and_proposers() -> None:
    assert set(TASK_EXECUTORS) == set(TaskKind) == set(TASK_PROPOSERS)
    executors = set().union(*TASK_EXECUTORS.values())
    # Only the two specialists execute tools; the reasoning roles never do.
    assert executors == {AgentRole.SQL_ANALYST, AgentRole.DOCUMENT_RESEARCH}
    proposers = set().union(*TASK_PROPOSERS.values())
    assert proposers == {AgentRole.SUPERVISOR, AgentRole.BUSINESS_INVESTIGATOR, AgentRole.CRITIC}


def _round_trip_models() -> list[BaseModel]:
    return [
        plan_of(
            sql_task("trend", evidence_refs=("ev_1",)),
            document_task("pricing"),
            hypothesis_task("h1-tenure", depends_on=("trend",)),
            hypothesis_task(
                "h3-docs",
                hypothesis_id="h3",
                method="document_check",
                fields=(),
                query="Competitor launch",
                assignee=AgentRole.DOCUMENT_RESEARCH,
            ),
        ),
        TaskOutcome(
            task_id="trend",
            status="succeeded",
            attempts=1,
            evidence_refs=(EvidenceRef(id="ev_1", kind="query"),),
        ),
        TaskOutcome(task_id="pricing", status="failed", error_category="timeout", attempts=2),
        TaskOutcome(
            task_id="h3", status="skipped", error_category="resource_unavailable", attempts=0
        ),
        EXECUTION_LIMITS,
        BudgetUsage(model_calls=3, input_tokens=900, output_tokens=300, elapsed_seconds=1.5),
        ProviderCostProfile(provider="gemini", model="gemini-2.5-flash", input_per_million_usd=0.3),
        CostEstimate(bound="indicative", amount_usd=0.1, limitations=("retries_not_metered",)),
    ]


@pytest.mark.parametrize("model", _round_trip_models(), ids=lambda model: type(model).__name__)
def test_models_round_trip_through_json_without_pickle(model: BaseModel) -> None:
    encoded = model.model_dump_json()
    assert type(model).model_validate_json(encoded) == model
    # The fallback provider validates parsed JSON as a Python dict (src/llm/fallback.py).
    assert type(model).model_validate(json.loads(encoded)) == model


def test_plan_schema_version_is_pinned() -> None:
    payload = json.loads(churn_plan().model_dump_json())
    assert payload["schema_version"] == SCHEMA_VERSION == 1
    payload["schema_version"] = 2
    with pytest.raises(ValidationError):
        InvestigationPlan.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    [(), ("tasks", 0), ("tasks", 0, "input")],
    ids=["plan", "task", "task-input"],
)
def test_unknown_fields_are_rejected_at_every_level(path: tuple[object, ...]) -> None:
    payload = json.loads(churn_plan().model_dump_json())
    target = payload
    for key in path:
        target = target[key]
    target["require_sql_approval"] = False
    with pytest.raises(ValidationError):
        InvestigationPlan.model_validate(payload)


def test_models_are_immutable() -> None:
    plan = churn_plan()
    with pytest.raises(ValidationError):
        plan.objective = "changed"


def test_task_input_kind_must_match_task_kind() -> None:
    payload = json.loads(sql_task("trend").model_dump_json())
    payload["kind"] = "document_research"
    with pytest.raises(ValidationError, match="must match"):
        PlanTask.model_validate(payload)


@pytest.mark.parametrize(
    "change",
    [
        {"tasks": [json.loads(sql_task(f"t{i}").model_dump_json()) for i in range(13)]},
        {"sub_questions": ["q"] * 7},
        {"sub_questions": []},
        {"objective": "x" * 501},
    ],
    ids=["13-tasks", "7-sub-questions", "no-sub-questions", "long-objective"],
)
def test_plan_size_caps(change: dict[str, object]) -> None:
    payload = {**json.loads(churn_plan().model_dump_json()), **change}
    with pytest.raises(ValidationError):
        InvestigationPlan.model_validate(payload)


@pytest.mark.parametrize(
    "task_id", ["", "Trend", "1trend", "trend task", "t" * 33, "ev_1;drop"], ids=repr
)
def test_task_ids_are_short_tokens(task_id: str) -> None:
    payload = json.loads(sql_task("trend").model_dump_json())
    payload["id"] = task_id
    with pytest.raises(ValidationError):
        PlanTask.model_validate(payload)


def test_task_inputs_are_bounded() -> None:
    with pytest.raises(ValidationError):
        SQLAnalysisInput(objective="x" * 301, fields=("churned",))
    with pytest.raises(ValidationError):
        SQLAnalysisInput(objective="Churn", fields=())
    with pytest.raises(ValidationError):
        SQLAnalysisInput(objective="Churn", fields=("churned",), evidence_refs=("not-an-id",))
    with pytest.raises(ValidationError):
        DocumentResearchInput(query="Pricing", purpose="policy", max_passages=9)
    with pytest.raises(ValidationError):
        DocumentResearchInput(query="Pricing", purpose="instructions")


@pytest.mark.parametrize(
    ("method", "fields", "query"),
    [
        ("document_check", ("churned",), "Competitor launch"),
        ("document_check", (), None),
        ("segment_comparison", (), None),
        ("segment_comparison", ("churned",), "Competitor launch"),
    ],
)
def test_hypothesis_test_inputs_match_their_method(
    method: str, fields: tuple[str, ...], query: str | None
) -> None:
    with pytest.raises(ValidationError):
        HypothesisTestInput.model_validate(
            {"hypothesis_id": "h1", "method": method, "fields": fields, "query": query}
        )


def test_task_outcome_categories_are_content_free() -> None:
    TaskOutcome(task_id="trend", status="refused", error_category="policy_refused", attempts=0)
    with pytest.raises(ValidationError):
        TaskOutcome(task_id="trend", status="failed", error_category="SELECT * FROM x", attempts=1)
    with pytest.raises(ValidationError):
        TaskOutcome(task_id="trend", status="failed", attempts=1)
    with pytest.raises(ValidationError):
        TaskOutcome(task_id="trend", status="succeeded", error_category="timeout", attempts=1)
    with pytest.raises(ValidationError):
        TaskOutcome(task_id="trend", status="succeeded", attempts=0)
    with pytest.raises(ValidationError):
        TaskOutcome(task_id="trend", status="failed", error_category="timeout", attempts=3)


# --- no unintended coercion (review of the non-strict model config) ---------------------------

_TASK = json.loads(sql_task("trend").model_dump_json())
_DOC_TASK = json.loads(document_task("pricing").model_dump_json())
_OUTCOME = {"task_id": "trend", "status": "succeeded", "attempts": 1}

COERCION_CASES: list[tuple[str, type[BaseModel], dict[str, object]]] = [
    ("numeric-string-int", PlanTask, {**_TASK, "max_attempts": "2"}),
    ("bool-as-int", PlanTask, {**_TASK, "max_attempts": True}),
    ("float-as-int", PlanTask, {**_TASK, "max_attempts": 1.0}),
    (
        "numeric-string-passages",
        PlanTask,
        {**_DOC_TASK, "input": {**_DOC_TASK["input"], "max_passages": "3"}},
    ),
    ("bool-attempts", TaskOutcome, {**_OUTCOME, "attempts": True}),
    ("string-attempts", TaskOutcome, {**_OUTCOME, "attempts": "1"}),
    ("enum-name-not-value", PlanTask, {**_TASK, "assignee": "SQL_ANALYST"}),
    ("enum-unknown", PlanTask, {**_TASK, "kind": "sql"}),
    ("enum-as-int", PlanTask, {**_TASK, "assignee": 1}),
    ("purpose-unknown", PlanTask, {**_DOC_TASK, "input": {**_DOC_TASK["input"], "purpose": "x"}}),
    ("field-ref-int", PlanTask, {**_TASK, "input": {**_TASK["input"], "fields": [1, 2]}}),
    (
        "field-ref-bare-string",
        PlanTask,
        {**_TASK, "input": {**_TASK["input"], "fields": "churned"}},
    ),
    ("field-ref-object", PlanTask, {**_TASK, "input": {**_TASK["input"], "fields": {"a": 1}}}),
    ("field-ref-nested", PlanTask, {**_TASK, "input": {**_TASK["input"], "fields": [["churned"]]}}),
    ("dependency-int", PlanTask, {**_TASK, "depends_on": [1]}),
    ("task-id-int", PlanTask, {**_TASK, "id": 7}),
    ("evidence-ref-int", TaskOutcome, {**_OUTCOME, "evidence_refs": [7]}),
    (
        "schema-version-string",
        InvestigationPlan,
        {**json.loads(churn_plan().model_dump_json()), "schema_version": "1"},
    ),
]


@pytest.mark.parametrize(
    ("model", "payload"), [case[1:] for case in COERCION_CASES], ids=[c[0] for c in COERCION_CASES]
)
def test_no_unintended_coercion_on_either_validation_path(
    model: type[BaseModel], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(payload))
