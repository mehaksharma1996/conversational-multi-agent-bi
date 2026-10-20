"""INV-6 S3 (#139): v2 planning drafts, Business Investigator proposer rules, and the D2 budget.

Planning still sees only the question, schema metadata, and document titles; plans are proposals
until a person approves them; hypothesis tests are validated as the Business Investigator's, never
the Supervisor's.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from packages.investigation.budget import EXECUTION_LIMITS, PLANNING_LIMITS
from packages.investigation.gateway import (
    PLANNING_STAGE_OUTPUT_TOKENS,
    ProviderAttemptBound,
    ReviewedPlanningCapability,
)
from packages.investigation.hypotheses import InvestigationPlanV2
from packages.investigation.lifecycle import InvestigationStatus
from packages.investigation.planning import (
    PlanningContext,
    validate_planning_draft,
)
from packages.investigation.state import InvestigationScope, plan_digest
from packages.investigation.validation import ResourceContext, validate_plan
from src.llm.bounded import admit_planning_adapter
from src.orchestration.investigation_proposal import InvestigationProposalRun
from src.orchestration.planning_prompt import (
    INITIAL_INSTRUCTIONS,
    PLANNING_PROMPT_VERSION,
    render_initial_prompt,
)
from tests.investigation_churn_schema import (
    CHURN_SCHEMA,
    churn_metadata,
    churn_v2_plan,
    churn_v2_raw,
    churn_v2_response,
)
from tests.investigation_fixtures import SCOPE, churn_plan
from tests.investigation_planning_fakes import (
    BOUND,
    Cancellation,
    FakeClock,
    LiveAuthority,
    ScriptedDeadlineAdapter,
    authority,
    evidence,
    pricing,
)

RESOURCES = ResourceContext(has_table=True, has_documents=True)


def churn_context() -> PlanningContext:
    return PlanningContext(
        scope=InvestigationScope.model_validate(SCOPE), metadata=churn_metadata()
    )


def violations(raw: dict[str, Any]) -> set[tuple[str, str | None]]:
    from packages.investigation.hypotheses import parse_plan

    plan = parse_plan(raw)
    found = validate_plan(plan, resources=RESOURCES, schema=CHURN_SCHEMA, limits=EXECUTION_LIMITS)
    return {(item.code, item.task_id) for item in found}


# --- drafts -------------------------------------------------------------------------------------


def test_the_churn_v2_draft_validates_against_the_real_schema() -> None:
    result = validate_planning_draft(json.loads(churn_v2_response()), context=churn_context())
    assert result.errors == ()
    assert result.draft is not None and type(result.draft.plan) is InvestigationPlanV2
    assert result.draft.plan == churn_v2_plan()


def test_a_v1_draft_still_validates_and_keeps_its_digest() -> None:
    candidate = {"plan": churn_plan().model_dump(mode="json"), "missing_information": []}
    from tests.investigation_planning_fakes import context

    result = validate_planning_draft(candidate, context=context())
    assert result.errors == () and result.draft is not None
    assert plan_digest(result.draft.plan) == plan_digest(churn_plan())


def test_an_outdated_fixture_name_is_an_unknown_field_on_the_real_schema() -> None:
    raw = churn_v2_raw()
    raw["tasks"][4]["input"]["filters"][0]["field"] = "segment"  # the old fixture name
    assert ("unknown_field", "h1_rate") in violations(raw)


# --- proposer rules (D1) --------------------------------------------------------------------------


def test_hypothesis_tests_are_the_business_investigators_not_the_supervisors() -> None:
    raw = churn_v2_raw()
    raw["hypotheses"][3]["tests"] = []  # h4_rate is no longer bound by any hypothesis
    raw["hypotheses"][3]["testability"] = "untestable"
    raw["hypotheses"][3]["missing_data"] = ["x"]
    found = violations(raw)
    assert ("hypothesis_unbound", "h4_rate") in found
    assert ("proposer_not_allowed", "h4_rate") in found  # checked as the Supervisor's own task


def test_a_v1_plan_still_cannot_contain_hypothesis_tests() -> None:
    from packages.investigation.contracts import InvestigationPlan
    from tests.investigation_fixtures import hypothesis_task, plan_of

    plan = plan_of(hypothesis_task("h1_check"))
    assert type(plan) is InvestigationPlan
    found = {
        item.code
        for item in validate_plan(
            plan, resources=RESOURCES, schema=CHURN_SCHEMA, limits=EXECUTION_LIMITS
        )
    }
    assert {"proposer_not_allowed", "unknown_hypothesis"} <= found


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda raw: raw["hypotheses"][0].update(tests=["h4_rate"]),
            ("hypothesis_task_mismatch", "h4_rate"),
        ),
        (
            lambda raw: raw["hypotheses"][0].update(tests=["trend"]),
            ("hypothesis_task_mismatch", "trend"),
        ),
        (
            lambda raw: raw["hypotheses"][0].update(context_tasks=["trend"]),
            ("unknown_context_task", "trend"),
        ),
        (
            lambda raw: raw["hypotheses"][0].update(context_tasks=["ghost"]),
            ("unknown_context_task", "ghost"),
        ),
        (lambda raw: raw.pop("test_defaults"), ("spec_incomplete", "h1_rate")),
        (
            lambda raw: raw["tasks"][4]["input"]["claim"].update(min_denominator=10),
            ("denominator_below_floor", "h1_rate"),
        ),
        (
            lambda raw: raw["tasks"][4]["input"].update(evidence_refs=["ev_abc"]),
            ("unknown_evidence", "h1_rate"),
        ),
        (
            lambda raw: raw["tasks"][4].update(assignee="document_research"),
            ("method_assignee_mismatch", "h1_rate"),
        ),
        (
            lambda raw: raw["test_defaults"]["metric"].update(event="cancel_flag"),
            ("unknown_field", "h1_rate"),
        ),
    ],
    ids=[
        "test-of-another-hypothesis",
        "evidence-task-as-test",
        "sql-task-as-context",
        "unknown-context",
        "no-defaults",
        "small-floor",
        "evidence-refs",
        "wrong-assignee",
        "unknown-metric-field",
    ],
)
def test_v2_hypothesis_violations(change: Any, expected: tuple[str, str]) -> None:
    raw = copy.deepcopy(churn_v2_raw())
    change(raw)
    assert expected in violations(raw)


def test_the_denominator_floor_is_configurable_but_never_below_the_spec() -> None:
    plan = churn_v2_plan()
    strict = validate_plan(
        plan,
        resources=RESOURCES,
        schema=CHURN_SCHEMA,
        limits=EXECUTION_LIMITS,
        min_denominator=100,
    )
    assert {item.code for item in strict} == {"denominator_below_floor"}


def test_missing_resources_still_refuse_hypothesis_tests() -> None:
    plan = churn_v2_plan()
    found = validate_plan(
        plan,
        resources=ResourceContext(has_table=False, has_documents=True),
        schema=CHURN_SCHEMA,
        limits=EXECUTION_LIMITS,
    )
    assert ("resource_unavailable", "h1_rate") in {(item.code, item.task_id) for item in found}


# --- the prompt -----------------------------------------------------------------------------------


def test_the_v2_prompt_is_versioned_and_sees_metadata_only() -> None:
    prompt = render_initial_prompt(churn_metadata())
    assert PLANNING_PROMPT_VERSION == "inv6-planning-prompt-v2"
    assert prompt.text.startswith("[inv6-planning-prompt-v2]")
    for forbidden in (SCOPE["tenant_id"], SCOPE["dataset_id"], "oracle", "reference.sql", "SELECT"):
        assert forbidden not in prompt.data
    assert "hypothesis_test" in INITIAL_INSTRUCTIONS and "never causes" in INITIAL_INSTRUCTIONS


# --- the D2 budget ------------------------------------------------------------------------------


def test_the_planning_output_budget_is_two_thousand_per_stage() -> None:
    assert PLANNING_STAGE_OUTPUT_TOKENS == 2_000 and PLANNING_LIMITS.output_tokens == 4_000
    assert PLANNING_LIMITS.output_tokens == 2 * PLANNING_STAGE_OUTPUT_TOKENS
    assert PLANNING_LIMITS.model_calls == 2 and PLANNING_LIMITS.wall_clock_seconds == 30.0


def test_capability_evidence_for_the_old_bound_fails_exact_admission() -> None:
    old = ProviderAttemptBound(
        provider=BOUND.provider,
        model=BOUND.model,
        data_recipient=None,
        max_attempts=1,
        max_output_tokens=1_000,  # the pre-INV-6 stage cap
        input_bounded=True,
    )
    with pytest.raises(ValidationError):
        ReviewedPlanningCapability(
            bound=old,
            evidence_version="scripted-review-v1",
            provider_deadline_enforced=True,
            termination_confirmed=True,
        )
    for cap in (1_000, 1_999, 2_001, 4_000):
        bound = BOUND.model_copy(update={"max_output_tokens": cap})
        with pytest.raises(ValidationError):
            ReviewedPlanningCapability(
                bound=bound,
                evidence_version="scripted-review-v1",
                provider_deadline_enforced=True,
                termination_confirmed=True,
            )
    assert admit_planning_adapter is not None  # exact-cap admission is enforced in bounded.py too


def test_the_cost_estimate_prices_the_new_output_limit() -> None:
    from decimal import ROUND_CEILING, Decimal

    from packages.investigation.gateway import ProviderEnvelope
    from packages.investigation.planning_cost import estimate_planning_cost

    estimate = estimate_planning_cost(ProviderEnvelope(members=(BOUND,)), pricing(), evidence())
    profile = pricing().profiles[0]
    raw = (
        12_000 * Decimal(str(profile.input_per_million_usd))
        + 4_000 * Decimal(str(profile.output_per_million_usd))
    ) / 1_000_000
    assert estimate.amount_usd == float(raw.quantize(Decimal("0.0001"), rounding=ROUND_CEILING))
    assert estimate.bound == "enforced_upper_bound"


# --- a v2 proposal reaches approval only ---------------------------------------------------------


def test_a_scripted_v2_proposal_awaits_approval_and_executes_nothing() -> None:
    run = InvestigationProposalRun(
        authority=authority(),
        context=churn_context(),
        adapter=ScriptedDeadlineAdapter(answers=[churn_v2_response()]),
        evidence=evidence(),
        pricing=pricing(),
        live=LiveAuthority(),
        cancellation=Cancellation(),
        clock=FakeClock(),
    )
    result = run.run()
    assert result.status is InvestigationStatus.AWAITING_PLAN_APPROVAL
    view = run.state()
    assert type(view.plan) is InvestigationPlanV2
    assert result.record.plan_digest == plan_digest(churn_v2_plan())
    assert view.plan_approval is None and view.tasks == {} and view.verdicts == {}
