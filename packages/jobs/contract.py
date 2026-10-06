"""Framework-neutral job contract (ADR 0020, issue #10).

A job record carries identity, ownership, state, progress, a safe failure category, an idempotency
fingerprint, and a correlation ID. It never carries user content: operation and progress-stage names
are validated tokens, failures are reduced to a category from the shared error vocabulary, and the
request fingerprint is a digest. The in-memory ``result`` is excluded from ``repr`` and comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

_TOKEN = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# FAILED is the only state that can leave a terminal position, and only to be retried (QUEUED).
TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.EXPIRED}),
    JobStatus.RUNNING: frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.SUCCEEDED: frozenset({JobStatus.EXPIRED}),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED, JobStatus.EXPIRED}),
    JobStatus.CANCELLED: frozenset({JobStatus.EXPIRED}),
    JobStatus.EXPIRED: frozenset(),
}
TERMINAL_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.EXPIRED}
)


class JobError(Exception):
    """Base class for job contract errors."""


class JobNotFoundError(JobError):
    """The job does not exist for this tenant (a foreign tenant's job looks identical)."""


class JobConflictError(JobError):
    """An illegal transition, an exhausted retry budget, or a reused idempotency key."""


class JobQueueFullError(JobError):
    """Admission refused: the executor is at its bounded capacity."""


class JobCancelledError(JobError):
    """Raised inside a handler (via the context) to acknowledge cancellation."""


def validate_token(kind: str, value: str) -> str:
    """Accept only short lowercase tokens so names can never smuggle user content."""
    if not _TOKEN.fullmatch(value):
        raise ValueError(f"{kind} must be a lowercase token such as 'documents.index'.")
    return value


@dataclass(frozen=True)
class JobRecord:
    id: str
    tenant_id: str
    workspace_id: str
    operation: str
    status: JobStatus
    request_id: str | None
    idempotency_key: str | None
    request_fingerprint: str | None
    attempt: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    progress_stage: str | None = None
    progress_completed: int = 0
    progress_total: int | None = None
    error_category: str | None = None
    cancel_requested: bool = False
    result: object | None = field(default=None, repr=False, compare=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def retryable(self) -> bool:
        return self.status is JobStatus.FAILED and self.attempt < self.max_attempts


@dataclass(frozen=True)
class JobEvent:
    """Content-free lifecycle notification suitable for allowlisted telemetry or audit."""

    job_id: str
    tenant_id: str
    workspace_id: str
    operation: str
    status: JobStatus
    attempt: int
    request_id: str | None
    error_category: str | None = None
    duration_ms: float | None = None
