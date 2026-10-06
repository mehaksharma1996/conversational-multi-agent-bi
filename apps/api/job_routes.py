"""Tenant-scoped HTTP surface for process-local jobs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from apps.api.authorization import Capability, requires
from apps.api.dependencies import get_identity, get_job_executor
from apps.api.errors import STANDARD_ERROR_RESPONSES, ApiError, ResourceNotFoundError
from apps.api.models import JobListResponse, JobResponse
from apps.api.pagination import decode_cursor, encode_cursor
from apps.api.serializers import job_response
from packages.connectors import IdentityContext
from packages.jobs import (
    InProcessJobExecutor,
    JobConflictError,
    JobNotFoundError,
    JobQueueFullError,
    JobRecord,
)

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

IdentityDependency = Annotated[IdentityContext, Depends(get_identity)]
JobExecutorDependency = Annotated[InProcessJobExecutor, Depends(get_job_executor)]


@router.get(
    "",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=JobListResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
def list_jobs(
    response: Response,
    identity: IdentityDependency,
    executor: JobExecutorDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> JobListResponse:
    """List jobs in stable creation order using an opaque exclusive cursor."""
    after = decode_cursor(cursor) if cursor is not None else None
    records, has_more = executor.list_page(identity.tenant_id, limit=limit, after=after)
    next_cursor = (
        encode_cursor(records[-1].created_at, records[-1].id) if has_more and records else None
    )
    response.headers["Cache-Control"] = "no-store"
    return JobListResponse(
        items=[job_response(record) for record in records],
        next_cursor=next_cursor,
    )


@router.get(
    "/{job_id}",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=JobResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
def get_job(
    job_id: str,
    response: Response,
    identity: IdentityDependency,
    executor: JobExecutorDependency,
) -> JobResponse:
    """Read content-free status and progress metadata for one owned job."""
    response.headers["Cache-Control"] = "no-store"
    return job_response(_owned_job(executor, job_id, identity.tenant_id))


@router.delete(
    "/{job_id}",
    dependencies=[requires(Capability.DATA_WRITE)],
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        **STANDARD_ERROR_RESPONSES,
        200: {"model": JobResponse, "description": "Job is already terminal"},
    },
)
def cancel_job(
    job_id: str,
    response: Response,
    identity: IdentityDependency,
    executor: JobExecutorDependency,
) -> JobResponse:
    """Request best-effort cancellation; repeated requests are safe and idempotent."""
    try:
        record = executor.cancel(job_id, identity.tenant_id)
    except JobNotFoundError:
        raise ResourceNotFoundError("Job") from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["Location"] = f"/api/v1/jobs/{record.id}"
    if record.is_terminal:
        response.status_code = status.HTTP_200_OK
    else:
        response.headers["Retry-After"] = "1"
    return job_response(record)


@router.post(
    "/{job_id}/retry",
    dependencies=[requires(Capability.DATA_WRITE)],
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=STANDARD_ERROR_RESPONSES,
)
def retry_job(
    job_id: str,
    response: Response,
    identity: IdentityDependency,
    executor: JobExecutorDependency,
) -> JobResponse:
    """Retry one failed owned job; the command is rejected in every other state."""
    try:
        record = executor.retry(job_id, identity.tenant_id)
    except JobNotFoundError:
        raise ResourceNotFoundError("Job") from None
    except JobConflictError:
        raise ApiError(
            409,
            "job_not_retryable",
            "The job is not failed with retry capacity remaining.",
        ) from None
    except JobQueueFullError:
        raise ApiError(
            503,
            "job_queue_full",
            "The job queue is full; retry this command later.",
            headers={"Retry-After": "1"},
        ) from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["Location"] = f"/api/v1/jobs/{record.id}"
    response.headers["Retry-After"] = "1"
    return job_response(record)


def _owned_job(executor: InProcessJobExecutor, job_id: str, tenant_id: str) -> JobRecord:
    try:
        return executor.get(job_id, tenant_id)
    except JobNotFoundError:
        raise ResourceNotFoundError("Job") from None
