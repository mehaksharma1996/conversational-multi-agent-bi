"""Read-only report and evidence reads for a finished investigation (UI-6c, #156).

The tenant comes from the verified identity; a foreign or unknown ID is the same 404. Content is
returned only after its digest and its typed contracts check out. Neither route creates, changes, or
promotes evidence. Capabilities are explained in ``docs/agents/briefs/156-ui6c-evidence-report.md``:
evidence needs ``workspace:read``, its SQL text needs ``analysis:run`` too, and an export needs
``report:export``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from apps.api.authorization import Capability, capabilities_for, requires
from apps.api.dependencies import get_identity, get_observability
from apps.api.errors import STANDARD_ERROR_RESPONSES, ApiError, ResourceNotFoundError
from apps.api.http_semantics import DOWNLOAD_SEMANTICS
from apps.api.investigation_content import Payload, QueryDetails, VerifiedContent
from apps.api.investigation_report_export import render_json, render_markdown
from apps.api.investigation_routes import RuntimeDependency, _owned
from apps.api.investigation_store import InvestigationContentError, InvestigationRecord
from apps.api.investigations import InvestigationRuntime
from apps.api.observability import ApiObservability
from packages.connectors import IdentityContext
from packages.investigation import InvestigationStatus
from packages.investigation.evidence import (
    DerivedEvidencePayload,
    DerivedRatio,
    DerivedStratum,
    DocumentEvidencePayload,
    document_citations_valid,
)
from packages.investigation.interim_report import MAX_REPORT_QUERY_ROWS
from packages.investigation.report import VerifiedReport
from packages.investigation.state import DerivedOperationName, EvidenceStatusName

router = APIRouter(prefix="/api/v1", tags=["investigations"])

IdentityDependency = Annotated[IdentityContext, Depends(get_identity)]
ObservabilityDependency = Annotated[ApiObservability, Depends(get_observability)]

SqlState = Literal["shown", "restricted", "not_recorded"]
ExportFormat = Literal["markdown", "json"]
_EXPORT_MEDIA = {"markdown": "text/markdown; charset=utf-8", "json": "application/json"}
_EXPORT_SUFFIX = {"markdown": "md", "json": "json"}


class QueryEvidenceItem(BaseModel):
    """Query evidence: its first rows (no more than the report shows) and how it was produced."""

    kind: Literal["query"] = "query"
    evidence_id: str
    task_id: str
    attempt: int
    columns: list[str]
    rows: list[list[str | int | float | bool | None]]
    row_count: int
    source_truncated: bool
    independently_confirmed: bool
    sql: str | None
    sql_state: SqlState
    policy_sql_status: str | None
    corrections_used: int | None


class DocumentSourceItem(BaseModel):
    """A retrieved source's location; the passage itself is never stored."""

    number: int
    filename: str
    page_number: int | None
    chunk_index: int | None
    relevance_score: float | None
    cited: bool


class DocumentEvidenceItem(BaseModel):
    """Document evidence as locations and statuses, never the model-written answer."""

    kind: Literal["document"] = "document"
    evidence_id: str
    task_id: str
    attempt: int
    citations_verified: bool
    sources: list[DocumentSourceItem]
    invalid_citations: list[int]
    evidence_statuses: list[EvidenceStatusName]


class DerivedEvidenceItem(BaseModel):
    """A fixed arithmetic operation over this task's own query evidence."""

    kind: Literal["derived"] = "derived"
    evidence_id: str
    task_id: str
    attempt: int
    operation: DerivedOperationName
    source_evidence_ids: list[str]
    measure: DerivedRatio
    reference: DerivedRatio
    value: float
    strata: list[DerivedStratum]
    standardized: Literal["measure", "reference"] | None


EvidenceItem = Annotated[
    QueryEvidenceItem | DocumentEvidenceItem | DerivedEvidenceItem,
    Field(discriminator="kind"),
]


class InvestigationEvidenceResponse(BaseModel):
    """The typed verified report and the evidence behind it, from one digest-checked read.

    ``evidence_state``: ``available``; ``not_reported`` for a failed run, whose report carries codes
    only; ``unavailable`` for a record whose stored digest does not cover its evidence.
    """

    investigation_id: str
    status: InvestigationStatus
    content_sha256: str
    report: VerifiedReport
    evidence_state: Literal["available", "not_reported", "unavailable"]
    evidence: list[EvidenceItem]


def _content(runtime: InvestigationRuntime, record: InvestigationRecord) -> VerifiedContent:
    try:
        content = runtime.verified_content(record)
    except InvestigationContentError:
        raise ApiError(
            409,
            "investigation_content_unverifiable",
            "This investigation's stored report could not be verified, so it is not shown.",
        ) from None
    if content is None:
        raise ResourceNotFoundError("Investigation report")
    return content


def _query_sql(details: QueryDetails | None, may_read_sql: bool) -> tuple[str | None, SqlState]:
    if details is None or details.sql is None:
        return None, "not_recorded"
    return (details.sql, "shown") if may_read_sql else (None, "restricted")


def _item(
    payload: Payload, details: QueryDetails | None, may_read_sql: bool, *, confirmed: bool
) -> QueryEvidenceItem | DocumentEvidenceItem | DerivedEvidenceItem:
    if isinstance(payload, DerivedEvidencePayload):
        return DerivedEvidenceItem(
            evidence_id=payload.evidence_id,
            task_id=payload.task_id,
            attempt=payload.attempt,
            operation=payload.operation,
            source_evidence_ids=list(payload.source_evidence_ids),
            measure=payload.measure,
            reference=payload.reference,
            value=payload.value,
            strata=list(payload.strata),
            standardized=payload.standardized,
        )
    if isinstance(payload, DocumentEvidencePayload):
        cited = set(payload.cited_source_numbers)
        return DocumentEvidenceItem(
            evidence_id=payload.evidence_id,
            task_id=payload.task_id,
            attempt=payload.attempt,
            citations_verified=document_citations_valid(payload),
            sources=[
                DocumentSourceItem(
                    number=source.number,
                    filename=source.filename,
                    page_number=source.page_number,
                    chunk_index=source.chunk_index,
                    relevance_score=source.relevance_score,
                    cited=source.number in cited,
                )
                for source in payload.sources
            ],
            invalid_citations=list(payload.invalid_citations),
            evidence_statuses=list(payload.evidence_statuses),
        )
    sql, sql_state = _query_sql(details, may_read_sql)
    return QueryEvidenceItem(
        evidence_id=payload.evidence_id,
        task_id=payload.task_id,
        attempt=payload.attempt,
        columns=list(payload.columns),
        rows=[list(row) for row in payload.rows[:MAX_REPORT_QUERY_ROWS]],
        row_count=len(payload.rows),
        source_truncated=payload.truncated,
        independently_confirmed=confirmed,
        sql=sql,
        sql_state=sql_state,
        policy_sql_status=details.policy_sql_status if details is not None else None,
        corrections_used=details.corrections_used if details is not None else None,
    )


@router.get(
    "/investigations/{investigation_id}/evidence",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=InvestigationEvidenceResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
def get_investigation_evidence(
    investigation_id: str,
    response: Response,
    identity: IdentityDependency,
    runtime: RuntimeDependency,
) -> InvestigationEvidenceResponse:
    """The verified report and its evidence. 404 until terminal or without a report; 409 if the
    stored content fails its digest or contracts. SQL text also needs ``analysis:run``."""
    record = _owned(runtime, investigation_id, identity.tenant_id)
    content = _content(runtime, record)
    response.headers["Cache-Control"] = "no-store"
    may_read_sql = Capability.ANALYSIS_RUN in capabilities_for(identity)
    confirmed = {
        table.evidence_id for table in content.report.query_tables if table.independently_confirmed
    }
    return InvestigationEvidenceResponse(
        investigation_id=record.id,
        status=record.status,
        content_sha256=content.content_sha256,
        report=content.report,
        evidence_state=content.evidence_state,
        evidence=[
            _item(
                payload,
                content.query_details.get(payload.evidence_id),
                may_read_sql,
                confirmed=payload.evidence_id in confirmed,
            )
            for payload in content.payloads
        ],
    )


_EXPORT_RESPONSES: dict[int | str, dict[str, object]] = {
    **STANDARD_ERROR_RESPONSES,
    200: {
        "description": "The verified report as an attachment. It contains no SQL.",
        "content": {
            "text/markdown": {"schema": {"type": "string"}},
            "application/json": {"schema": {"type": "string", "format": "binary"}},
        },
    },
}


@router.get(
    "/investigations/{investigation_id}/report/export",
    dependencies=[requires(Capability.REPORT_EXPORT)],
    response_class=Response,
    responses=_EXPORT_RESPONSES,
    description=DOWNLOAD_SEMANTICS,
)
def export_investigation_report(
    investigation_id: str,
    identity: IdentityDependency,
    runtime: RuntimeDependency,
    observability: ObservabilityDependency,
    export_format: Annotated[ExportFormat, Query(alias="format")] = "markdown",
) -> Response:
    """Download the digest-checked verified report. Same availability as the evidence read."""
    record = _owned(runtime, investigation_id, identity.tenant_id)
    content = _content(runtime, record)
    render = render_markdown if export_format == "markdown" else render_json
    payload = render(
        record.id, content.content_sha256, content.report, content.evidence_state
    ).encode("utf-8")
    observability.audit(
        "report.downloaded",
        identity,
        resource_id=record.id,
        format=export_format,
        size_bytes=len(payload),
    )
    filename = f"investigation-report-{record.id}.{_EXPORT_SUFFIX[export_format]}"
    return Response(
        content=payload,
        media_type=_EXPORT_MEDIA[export_format],
        headers={
            "Accept-Ranges": "none",
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
