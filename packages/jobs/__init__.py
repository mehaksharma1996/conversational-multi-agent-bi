"""Framework-neutral job contract and bounded in-process executor (ADR 0020)."""

from packages.jobs.contract import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    JobCancelledError,
    JobConflictError,
    JobError,
    JobEvent,
    JobNotFoundError,
    JobQueueFullError,
    JobRecord,
    JobStatus,
)
from packages.jobs.executor import InProcessJobExecutor, JobContext, JobExecutorStats
from packages.jobs.store import InMemoryJobStore

__all__ = [
    "TERMINAL_STATUSES",
    "TRANSITIONS",
    "InMemoryJobStore",
    "InProcessJobExecutor",
    "JobCancelledError",
    "JobConflictError",
    "JobContext",
    "JobError",
    "JobEvent",
    "JobExecutorStats",
    "JobNotFoundError",
    "JobQueueFullError",
    "JobRecord",
    "JobStatus",
]
