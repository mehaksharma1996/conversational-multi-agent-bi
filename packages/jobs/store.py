"""Tenant-scoped, in-memory job state with idempotency, expiry, and workspace purge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any
from uuid import uuid4

from packages.jobs.contract import (
    TRANSITIONS,
    JobConflictError,
    JobNotFoundError,
    JobRecord,
    JobStatus,
    validate_token,
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InMemoryJobStore:
    """Process-local state: nothing here survives a restart (see ADR 0010 and ADR 0020)."""

    def __init__(
        self,
        clock: Clock = _utc_now,
        default_ttl: timedelta = timedelta(hours=1),
    ) -> None:
        if default_ttl <= timedelta(0):
            raise ValueError("default_ttl must be positive.")
        self._clock = clock
        self._default_ttl = default_ttl
        self._lock = RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._keys: dict[tuple[str, str, str, str], str] = {}

    def submit(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        operation: str,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        max_attempts: int = 3,
        ttl: timedelta | None = None,
    ) -> tuple[JobRecord, bool]:
        """Create a queued job, or replay the existing one for the same idempotency scope.

        The scope is (tenant, workspace, operation, key). Replaying with a different request
        fingerprint is a conflict, never a silent reuse. Returns ``(record, created)``.
        """
        validate_token("operation", operation)
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        now = self._clock()
        with self._lock:
            if idempotency_key is not None:
                scope = (tenant_id, workspace_id, operation, idempotency_key)
                existing_id = self._keys.get(scope)
                existing = self._jobs.get(existing_id) if existing_id else None
                if existing is not None:
                    existing = self._expire_if_due_unlocked(existing)
                if existing is not None and existing.status is not JobStatus.EXPIRED:
                    if existing.request_fingerprint != request_fingerprint:
                        raise JobConflictError(
                            "The idempotency key was already used with a different request."
                        )
                    return existing, False
            record = JobRecord(
                id=uuid4().hex,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                operation=operation,
                status=JobStatus.QUEUED,
                request_id=request_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                attempt=1,
                max_attempts=max_attempts,
                created_at=now,
                updated_at=now,
                expires_at=now + (ttl or self._default_ttl),
            )
            self._jobs[record.id] = record
            if idempotency_key is not None:
                self._keys[(tenant_id, workspace_id, operation, idempotency_key)] = record.id
            return record, True

    def get(self, job_id: str, tenant_id: str) -> JobRecord:
        with self._lock:
            return self._expire_if_due_unlocked(self._owned_unlocked(job_id, tenant_id))

    def list_page(
        self,
        tenant_id: str,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[tuple[JobRecord, ...], bool]:
        """Return one tenant's jobs in stable creation order plus a next-page flag."""
        if limit < 1:
            raise ValueError("limit must be positive.")
        with self._lock:
            records = [record for record in self._jobs.values() if record.tenant_id == tenant_id]
            records.sort(key=lambda record: (record.created_at, record.id))
            if after is not None:
                records = [record for record in records if (record.created_at, record.id) > after]
            page = records[: limit + 1]
            return (
                tuple(self._expire_if_due_unlocked(record) for record in page[:limit]),
                len(page) > limit,
            )

    def transition(
        self,
        job_id: str,
        tenant_id: str,
        status: JobStatus,
        *,
        result: object | None = None,
        error_category: str | None = None,
    ) -> JobRecord:
        with self._lock:
            record = self._expire_if_due_unlocked(self._owned_unlocked(job_id, tenant_id))
            if status not in TRANSITIONS[record.status]:
                raise JobConflictError(f"A {record.status.value} job cannot become {status.value}.")
            changes: dict[str, Any] = {"status": status, "updated_at": self._clock()}
            if status is JobStatus.QUEUED:  # retry: a new attempt starts clean
                if record.attempt >= record.max_attempts:
                    raise JobConflictError("The retry budget for this job is exhausted.")
                changes.update(
                    attempt=record.attempt + 1,
                    error_category=None,
                    result=None,
                    progress_stage=None,
                    progress_completed=0,
                    progress_total=None,
                    cancel_requested=False,
                )
            elif status is JobStatus.SUCCEEDED:
                changes["result"] = result
            elif status is JobStatus.FAILED:
                changes["error_category"] = error_category or "internal"
            return self._store_unlocked(replace(record, **changes))

    def set_progress(
        self,
        job_id: str,
        tenant_id: str,
        stage: str,
        completed: int,
        total: int | None = None,
    ) -> JobRecord:
        validate_token("progress stage", stage)
        if completed < 0 or (total is not None and (total < 0 or completed > total)):
            raise ValueError("Progress must satisfy 0 <= completed <= total.")
        with self._lock:
            record = self._owned_unlocked(job_id, tenant_id)
            if record.status is not JobStatus.RUNNING:
                return record
            return self._store_unlocked(
                replace(
                    record,
                    progress_stage=stage,
                    progress_completed=completed,
                    progress_total=total,
                    updated_at=self._clock(),
                )
            )

    def request_cancel(self, job_id: str, tenant_id: str) -> JobRecord:
        """Flag a running job; a queued job becomes cancelled; a terminal job is unchanged."""
        with self._lock:
            record = self._expire_if_due_unlocked(self._owned_unlocked(job_id, tenant_id))
            if record.status is JobStatus.QUEUED:
                return self.transition(job_id, tenant_id, JobStatus.CANCELLED)
            if record.status is JobStatus.RUNNING and not record.cancel_requested:
                return self._store_unlocked(
                    replace(record, cancel_requested=True, updated_at=self._clock())
                )
            return record

    def discard(self, job_id: str, tenant_id: str) -> None:
        """Remove a job outright (used to roll back an admission that was refused)."""
        with self._lock:
            self._remove_unlocked(self._owned_unlocked(job_id, tenant_id))

    def purge_workspace(self, tenant_id: str, workspace_id: str) -> tuple[str, ...]:
        """Delete every job of one workspace (deletion and retention expiry); returns their IDs."""
        with self._lock:
            doomed = [
                record
                for record in self._jobs.values()
                if record.tenant_id == tenant_id and record.workspace_id == workspace_id
            ]
            for record in doomed:
                self._remove_unlocked(record)
            return tuple(record.id for record in doomed)

    def purge_expired(self) -> tuple[str, ...]:
        """Delete jobs past their expiry (running jobs are left to finish); returns their IDs."""
        now = self._clock()
        with self._lock:
            doomed = [
                record
                for record in self._jobs.values()
                if record.expires_at <= now and record.status is not JobStatus.RUNNING
            ]
            for record in doomed:
                self._remove_unlocked(record)
            return tuple(record.id for record in doomed)

    def _owned_unlocked(self, job_id: str, tenant_id: str) -> JobRecord:
        record = self._jobs.get(job_id)
        if record is None or record.tenant_id != tenant_id:
            raise JobNotFoundError("Job not found.")
        return record

    def _expire_if_due_unlocked(self, record: JobRecord) -> JobRecord:
        now = self._clock()
        if record.expires_at <= now and record.status not in {
            JobStatus.RUNNING,
            JobStatus.EXPIRED,
        }:
            return self._store_unlocked(
                replace(record, status=JobStatus.EXPIRED, result=None, updated_at=now)
            )
        return record

    def _store_unlocked(self, record: JobRecord) -> JobRecord:
        self._jobs[record.id] = record
        return record

    def _remove_unlocked(self, record: JobRecord) -> None:
        self._jobs.pop(record.id, None)
        if record.idempotency_key is not None:
            self._keys.pop(
                (record.tenant_id, record.workspace_id, record.operation, record.idempotency_key),
                None,
            )
