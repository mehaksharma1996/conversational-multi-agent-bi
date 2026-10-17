"""Builders for investigation contract tests, shaped like the approved churn scenario (#135)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from packages.investigation import (
    AgentRole,
    DocumentResearchInput,
    HypothesisTestInput,
    InvestigationPlan,
    PlanTask,
    SchemaContext,
    SQLAnalysisInput,
    TaskKind,
    TaskOutcome,
)
from packages.investigation.budget import BudgetLedger
from packages.investigation.gateway import (
    AuthorityStatus,
    ConsentSnapshot,
    InvestigationAuthority,
    ProviderAttemptBound,
    ProviderEnvelope,
)
from packages.investigation.state import (
    REDUCERS,
    STATE_VERSION,
    EvidenceRecord,
    InvestigationScope,
    PlanApprovalRecord,
    QueryProvenance,
    TaskRecord,
    Writer,
    owned_update,
    plan_digest,
    restore_state,
    unset_value,
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


# --- INV-3 (#136): JSON-native investigation state -------------------------------------------

SCOPE: dict[str, Any] = {
    "tenant_id": "tenant_a",
    "workspace_id": "ws_1",
    "investigation_id": "inv_1",
    "dataset_id": "ds_churn",
    "document_collection_id": "dc_policies",
}
APPROVED_AT = "2026-10-17T09:00:00+00:00"
APPROVAL_DEADLINE = "2026-10-18T09:00:00+00:00"

_FAILURE_CATEGORIES = {
    "failed": "internal",
    "skipped": "dependency_failed",
    "refused": "policy_refused",
    "cancelled": "cancelled",
}


def task_record(task_id: str, status: str = "pending", attempts: int = 0) -> dict[str, Any]:
    """A JSON task record; terminal statuses get a matching content-free outcome."""
    outcome = None
    if status not in {"pending", "running"}:
        outcome = TaskOutcome.model_validate(
            {
                "task_id": task_id,
                "status": status,
                "error_category": _FAILURE_CATEGORIES.get(status),
                "attempts": attempts,
            }
        )
    record = TaskRecord.model_validate(
        {"task_id": task_id, "status": status, "attempts": attempts, "outcome": outcome}
    )
    return dict(record.model_dump(mode="json"))


def evidence_record(task_id: str, *, attempt: int = 1, number: int = 1) -> dict[str, Any]:
    record = EvidenceRecord(
        id=f"ev_{number:032x}",
        kind="query",
        task_id=task_id,
        attempt=attempt,
        provenance=QueryProvenance(
            dataset_id=SCOPE["dataset_id"],
            sql_fingerprint="a" * 64,
            row_count=8,
            column_count=3,
            corrections_used=0,
            policy_sql_status="not_applicable",
            elapsed_ms=12,
        ),
    )
    return dict(record.model_dump(mode="json"))


def running_state(
    *,
    plan: InvestigationPlan | None = None,
    approved: bool = True,
    status: str = "running",
    with_evidence: bool = False,
    **tasks: dict[str, Any],
) -> dict[str, Any]:
    """A complete, valid JSON state for an approved churn investigation (revalidated before use)."""
    plan = plan or churn_plan()
    records = {task.id: task_record(task.id) for task in plan.tasks}
    records.update(tasks)
    evidence: dict[str, Any] = {}
    if with_evidence:
        if records["trend"]["status"] == "pending":
            records["trend"] = task_record("trend", "running", 1)
        record = evidence_record("trend")
        evidence[record["id"]] = record
    state: dict[str, Any] = {
        "state_version": STATE_VERSION,
        "scope": dict(SCOPE),
        "status": status,
        "plan": plan.model_dump(mode="json"),
        "tasks": records,
        "evidence": evidence,
        "decisions": [],
    }
    if approved:
        state["plan_approval"] = PlanApprovalRecord.model_validate(
            {
                "plan_digest": plan_digest(plan),
                "approved_at": APPROVED_AT,
                "deadline": APPROVAL_DEADLINE,
            }
        ).model_dump(mode="json")
    restore_state(state)
    return state


def apply_update(
    state: dict[str, Any],
    owner: Writer,
    update: dict[str, Any],
    *,
    ledger: BudgetLedger | None = None,
) -> dict[str, Any]:
    """What LangGraph does with a node's validated update: run each channel's reducer."""
    validated = owned_update(owner, state, update, ledger=ledger)
    merged = dict(state)
    for channel, value in validated.items():
        merged[channel] = REDUCERS[channel](state.get(channel, unset_value(channel)), value)
    return merged


# --- INV-3 (#136): trusted authority and its live check --------------------------------------


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class LiveAuthority:
    """A trusted adapter's live check that records what it was asked."""

    def __init__(self, status: Any = AuthorityStatus.VALID, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[InvestigationScope, int]] = []

    def current(self, scope: InvestigationScope, epoch: int) -> AuthorityStatus:
        self.calls.append((scope, epoch))
        if self.error is not None:
            raise self.error
        result: AuthorityStatus = self.status
        return result


def investigation_authority(
    *,
    plan: InvestigationPlan | None = None,
    deadline: datetime | str = APPROVAL_DEADLINE,
    **overrides: Any,
) -> InvestigationAuthority:
    """What the API adapter would grant an analyst for the approved churn plan."""
    plan = plan or churn_plan()
    fields: dict[str, Any] = {
        "scope": InvestigationScope.model_validate(SCOPE),
        "capabilities": frozenset({"analysis:run", "workspace:read"}),
        "consent": ConsentSnapshot(
            accepted_at=datetime(2026, 10, 17, 8, tzinfo=UTC),
            notice_version="v1",
            recipients=("Gemini",),
        ),
        "required_recipients": ("Gemini",),
        "approval": PlanApprovalRecord.model_validate(
            {"plan_digest": plan_digest(plan), "approved_at": APPROVED_AT, "deadline": deadline}
        ),
        "epoch": 3,
        "sql_approval_required": False,
        "provider_envelope": ProviderEnvelope(
            members=(
                ProviderAttemptBound(
                    provider="gemini",
                    model="gemini-2.5-flash",
                    data_recipient="Gemini",
                    max_attempts=3,
                    max_output_tokens=1_000,
                    input_bounded=True,
                ),
            )
        ),
    }
    return InvestigationAuthority(**(fields | overrides))
