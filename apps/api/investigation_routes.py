"""Tenant-scoped HTTP surface for investigations (ADR 0024, INV-11, #144).

Every endpoint takes the tenant from the verified identity and resolves records only through it, so
a foreign or unknown ID is the same 404. Bodies carry identifiers, digests, and choices only: the
question, scope, schema, approver, and provider configuration come from server-side state
(``apps/api/investigations.py``). Responses never contain SQL except the approver's own review of a
pending SQL wave (``GET .../sql-wave``), which requires ``analysis:run``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.authorization import Capability, capabilities_for, requires
from apps.api.dependencies import get_identity
from apps.api.errors import STANDARD_ERROR_RESPONSES, ApiError, ResourceNotFoundError
from apps.api.investigation_store import InvestigationNotFoundError, InvestigationRecord
from apps.api.investigations import (
    AUTHORITY_EPOCH,
    InvestigationRequestError,
    InvestigationRuntime,
)
from apps.api.repository import MessageRecord
from apps.api.serializers import is_investigation_proposal
from packages.connectors import IdentityContext
from packages.investigation import InvestigationStatus
from packages.investigation.approval import SQLItemDecision

router = APIRouter(prefix="/api/v1", tags=["investigations"])

IdentityDependency = Annotated[IdentityContext, Depends(get_identity)]

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Nonce = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
ResourceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]

# Fixed, reviewed wording for each explanation code a record may carry.
EXPLANATIONS: dict[str, str] = {
    "interrupted_by_restart": (
        "The service restarted before this investigation finished. Nothing resumes "
        "automatically; start a new investigation to try again."
    ),
    "cancelled_by_user": "This investigation was cancelled. Work already in flight was discarded.",
    "metering_unbounded": (
        "No model is configured that can run investigations within enforced limits."
    ),
    "job_queue_full": "The service was too busy to start this investigation.",
}
_GENERIC = "This investigation ended without a result."


def get_investigation_runtime(request: Request) -> InvestigationRuntime:
    runtime: InvestigationRuntime = request.app.state.investigations
    return runtime


RuntimeDependency = Annotated[InvestigationRuntime, Depends(get_investigation_runtime)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvestigationCreateRequest(_Body):
    message_id: ResourceId


class PlanDecisionBody(_Body):
    decision: Literal["approve", "reject"]
    plan_digest: Digest
    authority_epoch: Annotated[int, Field(ge=0)]
    follow_up_rounds: Annotated[int, Field(ge=0, le=2)] = 0


class SQLItemDecisionBody(_Body):
    task_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
    sql_fingerprint: Digest
    decision: Literal["approve", "reject", "edit"]
    edited_sql: Annotated[str, Field(min_length=1, max_length=20_000)] | None = None


class SQLWaveDecisionBody(_Body):
    wave_id: Nonce
    wave_digest: Digest
    items: Annotated[list[SQLItemDecisionBody], Field(min_length=1, max_length=24)]


class SQLWaveItemResponse(BaseModel):
    task_id: str
    attempt: int
    sql_fingerprint: str
    policy_status: str


class SQLWaveSummary(BaseModel):
    """A pending wave's identity: what a decision must echo. No SQL."""

    wave_id: str
    wave_digest: str
    sequence: int
    deadline: datetime
    items: list[SQLWaveItemResponse]


class SQLWaveReviewItem(SQLWaveItemResponse):
    sql: str


class SQLWaveReviewResponse(BaseModel):
    """The approver's review of a pending wave, including each statement's SQL."""

    wave_id: str
    wave_digest: str
    sequence: int
    deadline: datetime
    items: list[SQLWaveReviewItem]


class InvestigationResponse(BaseModel):
    id: str
    conversation_id: str
    message_id: str
    status: InvestigationStatus
    plan_digest: str | None = None
    authority_epoch: int | None = None
    explanation_code: str | None = None
    explanation: str | None = None
    terminal_category: str | None = None
    created_at: datetime
    updated_at: datetime
    plan: dict[str, Any] | None = None
    sql_wave: SQLWaveSummary | None = None
    report: dict[str, Any] | None = None


def _refused(error: InvestigationRequestError) -> ApiError:
    return ApiError(error.status_code, str(error.code), "The investigation request was refused.")


def _owned(
    runtime: InvestigationRuntime, investigation_id: str, tenant: str
) -> InvestigationRecord:
    try:
        return runtime.records.get(investigation_id, tenant)
    except InvestigationNotFoundError:
        raise ResourceNotFoundError("Investigation") from None


def _response(runtime: InvestigationRuntime, record: InvestigationRecord) -> InvestigationResponse:
    explanation = None
    if record.explanation_code is not None:
        explanation = EXPLANATIONS.get(record.explanation_code, _GENERIC)
    wave = None
    review = runtime.pending_wave(record.tenant_id, record.id)
    if review is not None:
        request = review.request
        wave = SQLWaveSummary(
            wave_id=request.wave_id,
            wave_digest=request.digest,
            sequence=request.sequence,
            deadline=request.deadline,
            items=[
                SQLWaveItemResponse(
                    task_id=item.task_id,
                    attempt=item.attempt,
                    sql_fingerprint=item.sql_fingerprint,
                    policy_status=item.policy_status,
                )
                for item in request.items
            ],
        )
    try:
        plan = runtime.plan(record)
        report = runtime.report(record)
    except Exception:  # unreadable or tampered content is never returned
        plan, report = None, None
    return InvestigationResponse(
        id=record.id,
        conversation_id=record.conversation_id,
        message_id=record.message_id,
        status=record.status,
        plan_digest=record.plan_digest,
        authority_epoch=AUTHORITY_EPOCH if record.plan_digest is not None else None,
        explanation_code=record.explanation_code,
        explanation=explanation,
        terminal_category=record.terminal_category,
        created_at=record.created_at,
        updated_at=record.updated_at,
        plan=dict(plan) if plan is not None else None,
        sql_wave=wave,
        report=dict(report) if report is not None else None,
    )


def _subject(identity: IdentityContext) -> str:
    """An opaque approver ID derived from the verified subject; never the subject itself."""
    subject = f"{identity.tenant_id}:{identity.subject or ''}"
    return "sub_" + hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]


def _capabilities(identity: IdentityContext) -> frozenset[str]:
    return frozenset(capability.value for capability in capabilities_for(identity))


@router.post(
    "/conversations/{conversation_id}/investigations",
    dependencies=[requires(Capability.ANALYSIS_RUN)],
    response_model=InvestigationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=STANDARD_ERROR_RESPONSES,
)
def create_investigation(
    conversation_id: str,
    command: InvestigationCreateRequest,
    request: Request,
    response: Response,
    identity: IdentityDependency,
    runtime: RuntimeDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> InvestigationResponse:
    """Start an investigation from an ``investigation_proposed`` message; planning runs as a job."""

    def source_is_proposal(message: MessageRecord) -> bool:
        return is_investigation_proposal(message)

    try:
        record, _ = runtime.create(
            tenant_id=identity.tenant_id,
            capabilities=_capabilities(identity),
            conversation_id=conversation_id,
            message_id=command.message_id,
            idempotency_key=idempotency_key,
            request_id=getattr(request.state, "request_id", None),
            source_is_proposal=source_is_proposal,
        )
    except InvestigationRequestError as error:
        raise _refused(error) from None
    response.headers["Cache-Control"] = "no-store"
    return _response(runtime, record)


@router.get(
    "/investigations/{investigation_id}",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=InvestigationResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
def get_investigation(
    investigation_id: str,
    response: Response,
    identity: IdentityDependency,
    runtime: RuntimeDependency,
) -> InvestigationResponse:
    """Status, progress, the plan awaiting approval, a pending wave's identity, and the report."""
    response.headers["Cache-Control"] = "no-store"
    return _response(runtime, _owned(runtime, investigation_id, identity.tenant_id))


@router.post(
    "/investigations/{investigation_id}/plan-decision",
    dependencies=[requires(Capability.ANALYSIS_RUN)],
    response_model=InvestigationResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
def decide_investigation_plan(
    investigation_id: str,
    command: PlanDecisionBody,
    identity: IdentityDependency,
    runtime: RuntimeDependency,
) -> InvestigationResponse:
    """Approve or reject the exact plan (digest and epoch) shown to this approver."""
    _owned(runtime, investigation_id, identity.tenant_id)
    try:
        record = runtime.decide_plan(
            tenant_id=identity.tenant_id,
            subject_id=_subject(identity),
            capabilities=_capabilities(identity),
            investigation_id=investigation_id,
            decision=command.decision,
            plan_digest_value=command.plan_digest,
            authority_epoch=command.authority_epoch,
            follow_up_rounds=command.follow_up_rounds,
        )
    except InvestigationRequestError as error:
        raise _refused(error) from None
    return _response(runtime, record)


@router.post(
    "/investigations/{investigation_id}/cancel",
    dependencies=[requires(Capability.ANALYSIS_RUN)],
    response_model=InvestigationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=STANDARD_ERROR_RESPONSES,
)
def cancel_investigation(
    investigation_id: str,
    identity: IdentityDependency,
    runtime: RuntimeDependency,
) -> InvestigationResponse:
    """Stop further work. Calls already in flight finish and their results are discarded."""
    _owned(runtime, investigation_id, identity.tenant_id)
    try:
        record = runtime.cancel(identity.tenant_id, investigation_id)
    except InvestigationRequestError as error:
        raise _refused(error) from None
    return _response(runtime, record)


@router.get(
    "/investigations/{investigation_id}/sql-wave",
    dependencies=[requires(Capability.ANALYSIS_RUN)],
    response_model=SQLWaveReviewResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
def get_investigation_sql_wave(
    investigation_id: str,
    response: Response,
    identity: IdentityDependency,
    runtime: RuntimeDependency,
) -> SQLWaveReviewResponse:
    """The pending SQL wave with each statement, for the approver; 404 when none is pending."""
    _owned(runtime, investigation_id, identity.tenant_id)
    review = runtime.pending_wave(identity.tenant_id, investigation_id)
    if review is None:
        raise ResourceNotFoundError("SQL wave")
    response.headers["Cache-Control"] = "no-store"
    request = review.request
    return SQLWaveReviewResponse(
        wave_id=request.wave_id,
        wave_digest=request.digest,
        sequence=request.sequence,
        deadline=request.deadline,
        items=[
            SQLWaveReviewItem(
                task_id=item.task_id,
                attempt=item.attempt,
                sql_fingerprint=item.sql_fingerprint,
                policy_status=item.policy_status,
                sql=review.statements[item.task_id],
            )
            for item in request.items
        ],
    )


@router.post(
    "/investigations/{investigation_id}/sql-wave",
    dependencies=[requires(Capability.ANALYSIS_RUN)],
    response_model=InvestigationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=STANDARD_ERROR_RESPONSES,
)
def decide_investigation_sql_wave(
    investigation_id: str,
    command: SQLWaveDecisionBody,
    identity: IdentityDependency,
    runtime: RuntimeDependency,
) -> InvestigationResponse:
    """Decide every statement of exactly the pending wave; the run resumes as a job."""
    _owned(runtime, investigation_id, identity.tenant_id)
    try:
        items = tuple(
            SQLItemDecision(
                task_id=item.task_id,
                sql_fingerprint=item.sql_fingerprint,
                decision=item.decision,
                edited_sql=item.edited_sql,
            )
            for item in command.items
        )
    except ValueError:
        raise ApiError(422, "sql_wave_decision_invalid", "The SQL decision is invalid.") from None
    try:
        record = runtime.decide_sql_wave(
            tenant_id=identity.tenant_id,
            subject_id=_subject(identity),
            capabilities=_capabilities(identity),
            investigation_id=investigation_id,
            wave_id=command.wave_id,
            wave_digest=command.wave_digest,
            items=items,
        )
    except InvestigationRequestError as error:
        raise _refused(error) from None
    return _response(runtime, record)
