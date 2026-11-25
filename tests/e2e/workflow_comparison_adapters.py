"""Scripted models for the EVAL-2 (#147) S1b comparison runner. NOT a live model.

Arm A is the bounded workflow (investigation routing off). Its ordinary question model is scripted
here per comparison case: the route, one SQL statement and, for policy questions, the predicates and
the quoted rule that a well-behaved single-query model would give. Everything else (routing, the SQL
guard, execution, insight rendering, citations, persistence) is the production application.

Arm B is the investigation workflow (routing on). Its planner and execution models are the existing
scripted investigation adapters: the browser journeys' churn adapters and the trajectory suite's
retail plans and SQL, served through the API's planning and execution providers instead of in
process.

What a run proves: how the application handles these model outputs. It says nothing about what any
real model would produce. Every adapter records the prompts it receives (in memory only), so the
runner can count calls and check that no evaluator-only text reached a model. No adapter reads an
evaluation file, and no adapter contacts a provider.

Development and test only: it imports test fakes, so nothing in ``src/``, ``apps/``, ``packages/``,
or ``scripts/`` may import it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.llm.base import LLMResponse
from tests.e2e.retail_app import (
    FULFILLMENT,
    REVIEW,
    ScriptedRetailLLM,
    _current_question,
)

CHURN_CASE = "churn-full-investigation"
TOP_REGION_CASE = "investigation-top-region-fulfillment"
REVIEW_RATE_CASE = "investigation-review-category-rate"
OCTOBER_CASE = "investigation-october-peak-explanation"
COMPARED_CASES = (CHURN_CASE, TOP_REGION_CASE, REVIEW_RATE_CASE, OCTOBER_CASE)

ARM_A_PROVIDER = "scripted-eval2-bounded"
ARM_A_MODEL = "scripted-eval2-bounded-v1"
ARM_B_MODEL = "scripted-investigation-adapters-v1"


# --- arm A: the bounded workflow's ordinary model ------------------------------------------------


@dataclass(frozen=True)
class BoundedScript:
    """What a single-query model says for one comparison question.

    ``policy_alias`` names the retail sample question whose documented predicates and quoted rule
    also apply, so the existing scripted criteria and document answers are reused unchanged.
    """

    route: str
    sql: str
    policy_alias: str | None = None


def bounded_scripts() -> dict[str, BoundedScript]:
    """Per case: the route, the one SQL statement, and the policy it relies on, if any."""
    from tests.e2e.investigation_trajectory_scenarios import RETAIL_SQL
    from tests.investigation_hypothesis_fakes import TREND_SQL

    return {
        CHURN_CASE: BoundedScript("sql", TREND_SQL),
        TOP_REGION_CASE: BoundedScript(
            "hybrid", RETAIL_SQL["Transactions at the escalation rule"], FULFILLMENT
        ),
        REVIEW_RATE_CASE: BoundedScript(
            "hybrid", RETAIL_SQL["Breach rate of the top category"], REVIEW
        ),
        OCTOBER_CASE: BoundedScript("sql", RETAIL_SQL["Revenue per month"]),
    }


class ScriptedBoundedComparisonLLM(ScriptedRetailLLM):
    """The retail demo's scripted model, extended with one script per comparison question.

    A question it has no script for falls back to the retail demo script. Every prompt is kept in
    memory for the leak check; nothing is written anywhere.
    """

    provider = ARM_A_PROVIDER
    model = ARM_A_MODEL
    configured = True

    def __init__(self, scripts_by_question: Mapping[str, BoundedScript]) -> None:
        self._scripts = dict(scripts_by_question)
        self.prompts: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def generate(self, prompt: str) -> LLMResponse:
        self.prompts.append(prompt)
        script = self._scripts.get(_current_question(prompt) or "")
        if script is None:
            return super().generate(prompt)
        if "Classify a business-intelligence question" in prompt:
            text = json.dumps({"route": script.route, "confidence": 1})
        elif "Extract business criteria" in prompt:
            text = json.dumps(self._criteria(prompt, script.policy_alias))
        elif "careful document analyst" in prompt:
            text = self._document_answer(prompt, script.policy_alias)
        else:
            text = script.sql
        return LLMResponse(text=text, model=self.model, provider=self.provider)


# --- arm B: the investigation workflow's planner and execution models -----------------------------

# EVAL-2's own wording for the scripted churn planner's hypotheses (S1c). The shared fixture
# (``tests/investigation_churn_schema.py``) words them exactly as the oracle words its claims, so
# that wording reached later prompts as model output. Only this local copy is reworded: the IDs,
# tests, context tasks, SQL, and approvals are unchanged, and so is what each hypothesis means.
EVAL2_CHURN_HYPOTHESES: dict[str, str] = {
    "h1": "Basic-plan SMB customers churned more in the quarter the Basic price went up.",
    "h2": "APAC customers churned more while support took longer to resolve their tickets.",
    "h3": "Customers given the retention playbook churned less than other customers.",
    "h4": "Enterprise customers churned more in the second quarter of 2026 than in the first.",
}


def eval2_churn_plan_response() -> str:
    """The scripted churn plan with EVAL-2's hypothesis wording; everything else is unchanged."""
    from tests.investigation_critique_fakes import churn_v7_response

    body = json.loads(churn_v7_response())
    hypotheses = body["plan"]["hypotheses"]
    if {item["id"] for item in hypotheses} != set(EVAL2_CHURN_HYPOTHESES):
        raise ValueError("the scripted churn plan's hypotheses changed; review EVAL-2's wording")
    for item in hypotheses:
        item["statement"] = EVAL2_CHURN_HYPOTHESES[item["id"]]
    return json.dumps(body, separators=(",", ":"))


def _retail_plans() -> dict[str, Callable[[], dict[str, Any]]]:
    from tests.e2e import investigation_trajectory_scenarios as scenarios

    return {
        TOP_REGION_CASE: scenarios.top_region_fulfillment_plan,
        REVIEW_RATE_CASE: scenarios.review_category_rate_plan,
        OCTOBER_CASE: scenarios.october_peak_plan,
    }


def _retail_execution_model(case_id: str) -> Any:
    from tests.e2e import investigation_trajectory_scenarios as scenarios

    if case_id == OCTOBER_CASE:
        return scenarios.scripted(scenarios.OCTOBER_SQL)
    return scenarios.scripted(scenarios.RETAIL_SQL, answer=scenarios._RETAIL_ANSWER)


class ComparisonInvestigationAdapters:
    """The scripted investigation adapters for one case, recording every planner and model.

    The API asks for a new planning provider per proposal and a new execution provider per run, as
    it does in production; each one built is kept so its prompts and calls can be counted.
    """

    def __init__(self, case_id: str) -> None:
        if case_id not in COMPARED_CASES:
            raise ValueError(f"no scripted investigation adapters for {case_id}")
        self.case_id = case_id
        self.planners: list[Any] = []
        self.models: list[Any] = []

    def planning(self) -> Any:
        from apps.api.investigations import PlanningProvider
        from tests.investigation_planning_fakes import (
            ENVELOPE,
            ScriptedDeadlineAdapter,
            evidence,
            pricing,
        )

        if self.case_id == CHURN_CASE:
            # The browser journeys' churn planning provider, answering with EVAL-2's wording.
            text = eval2_churn_plan_response()
        else:
            plan = _retail_plans()[self.case_id]()
            text = json.dumps({"plan": plan, "missing_information": []})
        provider = PlanningProvider(
            adapter=ScriptedDeadlineAdapter(answers=[text]),
            evidence=evidence(),
            pricing=pricing(),
            envelope=ENVELOPE,
        )
        self.planners.append(provider.adapter)
        return provider

    def execution(self) -> Any:
        from apps.api.investigations import ExecutionProvider
        from src.orchestration.tool_gateway import SCRIPTED_EXECUTION_ADMISSION
        from tests.e2e.investigation_app import ScriptedChurnReportAdapters
        from tests.investigation_report_fakes import NARRATIVE_MAX_INPUT
        from tests.test_investigation_tool_gateway import EXACT_ENVELOPE

        if self.case_id == CHURN_CASE:
            provider = ScriptedChurnReportAdapters().execution()
        else:
            provider = ExecutionProvider(
                llm_client=_retail_execution_model(self.case_id),
                envelope=EXACT_ENVELOPE,
                max_input_tokens_per_call=NARRATIVE_MAX_INPUT,
                scripted_admission=SCRIPTED_EXECUTION_ADMISSION,
            )
        self.models.append(provider.llm_client)
        return provider

    def prompts(self) -> list[str]:
        """Every prompt any planner or execution model received, in memory."""
        seen: list[str] = []
        for planner in self.planners:
            seen.extend(call.prompt for call in planner.calls)
            seen.extend(planner.counted_prompts)
        for model in self.models:
            seen.extend(model.prompts)
        return seen

    def planner_outputs(self) -> list[str]:
        """What the scripted planners answered: model output, authored in the test fixtures."""
        return [answer for planner in self.planners for answer in planner.answers]

    @property
    def calls(self) -> int:
        """Generation calls: one per planner call and one per execution-model prompt."""
        return sum(len(planner.calls) for planner in self.planners) + sum(
            len(model.prompts) for model in self.models
        )
