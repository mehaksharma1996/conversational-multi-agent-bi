"""Bounded in-process job execution (ADR 0020).

Concurrency is capped (``max_workers`` running plus ``max_queued`` waiting) so memory is bounded;
admission beyond that raises ``JobQueueFullError``. Handlers receive a ``JobContext`` for progress
and cooperative cancellation. Work closures (which may hold user content) live only in this process,
are never placed in records, events, or logs, and are dropped when a job can no longer be retried.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from time import perf_counter

from packages.jobs.contract import (
    JobCancelledError,
    JobConflictError,
    JobEvent,
    JobNotFoundError,
    JobQueueFullError,
    JobRecord,
    JobStatus,
)
from packages.jobs.store import InMemoryJobStore
from packages.observability.context import bind_request_id
from packages.observability.errors import error_category

LOGGER = logging.getLogger(__name__)

Observer = Callable[[JobEvent], None]


@dataclass(frozen=True)
class JobContext:
    """What a running handler may do: report progress and honour cancellation."""

    job_id: str
    tenant_id: str
    workspace_id: str
    attempt: int
    request_id: str | None
    _store: InMemoryJobStore

    def progress(self, stage: str, completed: int, total: int | None = None) -> None:
        try:
            self._store.set_progress(self.job_id, self.tenant_id, stage, completed, total)
        except JobNotFoundError:
            raise JobCancelledError from None

    @property
    def cancel_requested(self) -> bool:
        try:
            return self._store.get(self.job_id, self.tenant_id).cancel_requested
        except JobNotFoundError:  # the workspace (and its jobs) was purged underneath us
            return True

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise JobCancelledError


Work = Callable[[JobContext], object]


@dataclass(frozen=True)
class JobExecutorStats:
    """Admission counters: ``queued + running`` never exceeds ``capacity``."""

    queued: int
    running: int
    capacity: int
    workers: int


class InProcessJobExecutor:
    def __init__(
        self,
        store: InMemoryJobStore,
        *,
        max_workers: int = 2,
        max_queued: int = 8,
        observer: Observer | None = None,
    ) -> None:
        if max_workers < 1 or max_queued < 0:
            raise ValueError("max_workers must be at least 1 and max_queued at least 0.")
        self._store = store
        self._capacity = max_workers + max_queued
        self._observer = observer
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self._lock = Lock()
        self._work: dict[str, Work] = {}
        # Keyed by job, tagged with the attempt, so a finishing attempt can never release the
        # slot or future of a retry that was started a moment after it failed.
        self._futures: dict[str, tuple[int, Future[None]]] = {}
        self._active: dict[str, int] = {}
        self._max_workers = max_workers
        self._running = 0

    def stats(self) -> JobExecutorStats:
        """Point-in-time admission state for scrape-time gauges; carries no job identity."""
        with self._lock:
            active = len(self._active)
            return JobExecutorStats(
                queued=max(0, active - self._running),
                running=self._running,
                capacity=self._capacity,
                workers=self._max_workers,
            )

    def submit(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        operation: str,
        work: Work,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        max_attempts: int = 3,
        ttl: timedelta | None = None,
    ) -> tuple[JobRecord, bool]:
        """Admit a job; a replayed idempotency key returns the existing job and runs nothing."""
        with self._lock:
            record, created = self._store.submit(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                operation=operation,
                request_id=request_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                max_attempts=max_attempts,
                ttl=ttl,
            )
            if not created:
                return record, False
            if len(self._active) >= self._capacity:
                self._store.discard(record.id, tenant_id)
                raise JobQueueFullError("The job queue is full; try again later.")
            self._work[record.id] = work
            self._emit(record)  # before enqueueing, so `queued` always precedes `running`
            self._enqueue_unlocked(record)
        return record, True

    def get(self, job_id: str, tenant_id: str) -> JobRecord:
        record = self._store.get(job_id, tenant_id)
        if record.status is JobStatus.EXPIRED:
            self._forget(job_id)
        return record

    def list_page(
        self,
        tenant_id: str,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[tuple[JobRecord, ...], bool]:
        records, has_more = self._store.list_page(tenant_id, limit=limit, after=after)
        for record in records:
            if record.status is JobStatus.EXPIRED:
                self._forget(record.id)
        return records, has_more

    def cancel(self, job_id: str, tenant_id: str) -> JobRecord:
        """Best effort: queued jobs are cancelled now; running jobs stop when they next check."""
        record = self._store.request_cancel(job_id, tenant_id)
        if record.status is JobStatus.CANCELLED:
            self._forget(job_id)
            self._emit(record)
        return record

    def retry(self, job_id: str, tenant_id: str) -> JobRecord:
        """Start another attempt of a failed job; any other state is a conflict."""
        with self._lock:
            if job_id not in self._work:
                self._store.get(job_id, tenant_id)  # NotFound for a foreign or purged job
                raise JobConflictError("This job can no longer be retried.")
            if len(self._active) >= self._capacity:
                raise JobQueueFullError("The job queue is full; try again later.")
            record = self._store.transition(job_id, tenant_id, JobStatus.QUEUED)
            self._emit(record)
            self._enqueue_unlocked(record)
        return record

    def wait(self, job_id: str, tenant_id: str, timeout: float | None = None) -> JobRecord:
        self._store.get(job_id, tenant_id)
        entry = self._futures.get(job_id)
        if entry is not None:
            entry[1].result(timeout=timeout)
        return self._store.get(job_id, tenant_id)

    def purge_workspace(self, tenant_id: str, workspace_id: str) -> int:
        """Drop a workspace's jobs and work closures; running handlers see themselves cancelled."""
        removed = self._store.purge_workspace(tenant_id, workspace_id)
        for job_id in removed:
            self._forget(job_id)
        return len(removed)

    def purge_expired(self) -> int:
        removed = self._store.purge_expired()
        for job_id in removed:
            self._forget(job_id)
        return len(removed)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=True)

    def _enqueue_unlocked(self, record: JobRecord) -> None:
        self._active[record.id] = record.attempt
        self._futures[record.id] = (
            record.attempt,
            self._pool.submit(self._run, record.id, record.tenant_id, record.attempt),
        )

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._work.pop(job_id, None)

    def _release(self, job_id: str, attempt: int) -> None:
        with self._lock:
            if self._active.get(job_id) == attempt:
                del self._active[job_id]
            entry = self._futures.get(job_id)
            if entry is not None and entry[0] == attempt:
                del self._futures[job_id]

    def _run(self, job_id: str, tenant_id: str, attempt: int) -> None:
        counted_running = False
        try:
            try:
                running = self._store.transition(job_id, tenant_id, JobStatus.RUNNING)
            except (JobConflictError, JobNotFoundError):
                return  # cancelled, expired, or purged while queued
            with self._lock:
                self._running += 1
            counted_running = True
            self._emit(running)
            with self._lock:
                work = self._work.get(job_id)
            if work is None:
                self._finish(job_id, tenant_id, JobStatus.FAILED, perf_counter(), error="internal")
                return
            context = JobContext(
                job_id=job_id,
                tenant_id=tenant_id,
                workspace_id=running.workspace_id,
                attempt=running.attempt,
                request_id=running.request_id,
                _store=self._store,
            )
            started = perf_counter()
            correlation = (
                bind_request_id(running.request_id) if running.request_id else nullcontext()
            )
            try:
                with correlation:
                    result = work(context)
            except JobCancelledError:
                self._finish(job_id, tenant_id, JobStatus.CANCELLED, started)
                self._forget(job_id)
            except Exception as exc:  # noqa: BLE001 - reduced to a safe category below
                category = error_category(exc)
                LOGGER.info("job_failed operation=%s category=%s", running.operation, category)
                self._finish(job_id, tenant_id, JobStatus.FAILED, started, error=category)
            else:
                self._finish(job_id, tenant_id, JobStatus.SUCCEEDED, started, result=result)
                self._forget(job_id)
        finally:
            if counted_running:
                with self._lock:
                    self._running -= 1
            self._release(job_id, attempt)

    def _finish(
        self,
        job_id: str,
        tenant_id: str,
        status: JobStatus,
        started: float,
        *,
        result: object | None = None,
        error: str | None = None,
    ) -> None:
        try:
            record = self._store.transition(
                job_id, tenant_id, status, result=result, error_category=error
            )
        except (JobConflictError, JobNotFoundError):
            return  # purged while running: discard the outcome
        if record.attempt >= record.max_attempts and status is JobStatus.FAILED:
            self._forget(job_id)  # no retries left, so release the work closure
        self._emit(record, duration_ms=round((perf_counter() - started) * 1_000, 3))

    def _emit(self, record: JobRecord, duration_ms: float | None = None) -> None:
        if self._observer is None:
            return
        event = JobEvent(
            job_id=record.id,
            tenant_id=record.tenant_id,
            workspace_id=record.workspace_id,
            operation=record.operation,
            status=record.status,
            attempt=record.attempt,
            request_id=record.request_id,
            error_category=record.error_category,
            duration_ms=duration_ms,
        )
        try:
            self._observer(event)
        except Exception:  # noqa: BLE001 - an observer must never break job execution
            LOGGER.debug("job_observer_failed")
