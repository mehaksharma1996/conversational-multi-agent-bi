"""Builders for investigation contract tests, shaped like the approved churn scenario (#135)."""

from __future__ import annotations

from packages.investigation import (
    AgentRole,
    DocumentResearchInput,
    HypothesisTestInput,
    InvestigationPlan,
    PlanTask,
    SchemaContext,
    SQLAnalysisInput,
    TaskKind,
)

CHURN_SCHEMA = SchemaContext.of(
    [
        "customer_id",
        "quarter",
        "region",
        "segment",
        "plan_tier",
        "tenure_months",
        "avg_resolution_hours",
        "churned",
    ]
)


def sql_task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    fields: tuple[str, ...] = ("quarter", "segment", "churned"),
    evidence_refs: tuple[str, ...] = (),
    assignee: AgentRole = AgentRole.SQL_ANALYST,
) -> PlanTask:
    return PlanTask(
        id=task_id,
        kind=TaskKind.SQL_ANALYSIS,
        assignee=assignee,
        depends_on=depends_on,
        input=SQLAnalysisInput(
            objective="Compare churn rates by segment for the last two quarters.",
            fields=fields,
            evidence_refs=evidence_refs,
        ),
        success_criterion="Churn rate per segment and quarter with customer counts.",
    )


def document_task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    assignee: AgentRole = AgentRole.DOCUMENT_RESEARCH,
) -> PlanTask:
    return PlanTask(
        id=task_id,
        kind=TaskKind.DOCUMENT_RESEARCH,
        assignee=assignee,
        depends_on=depends_on,
        input=DocumentResearchInput(
            query="Pricing changes effective last quarter", purpose="policy"
        ),
        success_criterion="Cited policy passages, or a stated absence of evidence.",
    )


def hypothesis_task(
    task_id: str,
    *,
    hypothesis_id: str = "h1",
    method: str = "stratified_comparison",
    fields: tuple[str, ...] = ("plan_tier", "tenure_months", "churned"),
    query: str | None = None,
    assignee: AgentRole = AgentRole.SQL_ANALYST,
    depends_on: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
) -> PlanTask:
    return PlanTask(
        id=task_id,
        kind=TaskKind.HYPOTHESIS_TEST,
        assignee=assignee,
        depends_on=depends_on,
        input=HypothesisTestInput.model_validate(
            {
                "hypothesis_id": hypothesis_id,
                "method": method,
                "fields": fields,
                "query": query,
                "evidence_refs": evidence_refs,
            }
        ),
        success_criterion="A verdict that cites evidence.",
    )


def plan_of(*tasks: PlanTask) -> InvestigationPlan:
    return InvestigationPlan(
        objective="Why did churn increase last quarter, and what should management do?",
        sub_questions=(
            "How much did churn change?",
            "Which segments contributed most?",
            "What do the policy documents say?",
        ),
        tasks=tasks,
    )


def churn_plan() -> InvestigationPlan:
    return plan_of(
        sql_task("trend", fields=("quarter", "churned")),
        sql_task("segments", depends_on=("trend",)),
        document_task("pricing"),
        document_task("playbook"),
    )
