"""Contract tests for the job abstraction and bounded in-process executor (issue #10, ADR 0020)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from packages.jobs import (
    TRANSITIONS,
    InMemoryJobStore,
    InProcessJobExecutor,
    JobCancelledError,
    JobConflictError,
    JobContext,
    JobEvent,
    JobNotFoundError,
    JobQueueFullError,
    JobStatus,
)
from packages.observability import bind_request_id, current_request_id

TENANT = "tenant-a"
OTHER = "tenant-b"
WORKSPACE = "workspace-1"
WAIT = 5.0


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def events() -> list[JobEvent]:
    return []


@pytest.fixture
def make(clock: Clock, events: list[JobEvent]) -> Iterator:
    created: list[InProcessJobExecutor] = []

    def factory(max_workers: int = 1, max_queued: int = 1, ttl_minutes: int = 60):
        store = InMemoryJobStore(clock=clock, default_ttl=timedelta(minutes=ttl_minutes))
        executor = InProcessJobExecutor(
            store, max_workers=max_workers, max_queued=max_queued, observer=events.append
        )
        created.append(executor)
        return store, executor

    yield factory
    for executor in created:
        executor.shutdown(wait=True)


def _submit(executor: InProcessJobExecutor, work, **kwargs):
    kwargs.setdefault("tenant_id", TENANT)
    kwargs.setdefault("workspace_id", WORKSPACE)
    kwargs.setdefault("operation", "documents.index")
    return executor.submit(work=work, **kwargs)


def test_transition_table_only_lets_failed_jobs_retry() -> None:
    assert TRANSITIONS[JobStatus.EXPIRED] == frozenset()
    for final in (JobStatus.SUCCEEDED, JobStatus.CANCELLED):
        assert TRANSITIONS[final] == {JobStatus.EXPIRED}
    assert JobStatus.QUEUED in TRANSITIONS[JobStatus.FAILED]
    assert all(JobStatus.QUEUED not in TRANSITIONS[s] for s in JobStatus if s != JobStatus.FAILED)


def test_store_rejects_illegal_transitions_and_bad_tokens(clock: Clock) -> None:
    store = InMemoryJobStore(clock=clock)
    record, created = store.submit(tenant_id=TENANT, workspace_id=WORKSPACE, operation="a.b")
    assert created and record.status is JobStatus.QUEUED
    with pytest.raises(JobConflictError):
        store.transition(record.id, TENANT, JobStatus.SUCCEEDED)
    with pytest.raises(ValueError):
        store.submit(tenant_id=TENANT, workspace_id=WORKSPACE, operation="Index My Docs!")
    store.transition(record.id, TENANT, JobStatus.RUNNING)
    with pytest.raises(ValueError):
        store.set_progress(record.id, TENANT, "free text with spaces", 1, 2)
    with pytest.raises(ValueError):
        store.set_progress(record.id, TENANT, "extract", 3, 2)


def test_successful_job_reports_progress_result_and_correlation(make, events) -> None:
    _, executor = make()
    seen: dict[str, object] = {}

    def work(context: JobContext) -> str:
        context.progress("extract", 1, 2)
        seen["request_id"] = current_request_id()
        seen["attempt"] = context.attempt
        return "done"

    with bind_request_id("req-123"):
        record, created = _submit(executor, work, request_id="req-123")
    assert created and record.status is JobStatus.QUEUED

    finished = executor.wait(record.id, TENANT, WAIT)

    assert finished.status is JobStatus.SUCCEEDED
    assert finished.result == "done"
    assert (finished.progress_stage, finished.progress_completed, finished.progress_total) == (
        "extract",
        1,
        2,
    )
    assert seen == {"request_id": "req-123", "attempt": 1}
    assert [event.status for event in events] == [
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.SUCCEEDED,
    ]
    assert {event.request_id for event in events} == {"req-123"}
    assert events[-1].duration_ms is not None


def test_jobs_are_invisible_across_tenants(make) -> None:
    _, executor = make()
    record, _ = _submit(executor, lambda _context: "x")
    executor.wait(record.id, TENANT, WAIT)

    for action in (executor.get, executor.cancel, executor.retry):
        with pytest.raises(JobNotFoundError):
            action(record.id, OTHER)
    with pytest.raises(JobNotFoundError):
        executor.wait(record.id, OTHER, 1)
    assert executor.get(record.id, TENANT).status is JobStatus.SUCCEEDED


def test_idempotency_replays_without_rerunning_and_rejects_changed_requests(make) -> None:
    _, executor = make()
    runs = []

    def work(_context: JobContext) -> str:
        runs.append(1)
        return "ok"

    first, created = _submit(executor, work, idempotency_key="k1", request_fingerprint="fp-a")
    executor.wait(first.id, TENANT, WAIT)
    replay, replay_created = _submit(
        executor, work, idempotency_key="k1", request_fingerprint="fp-a"
    )

    assert created and not replay_created and replay.id == first.id
    assert replay.status is JobStatus.SUCCEEDED and len(runs) == 1
    with pytest.raises(JobConflictError):
        _submit(executor, work, idempotency_key="k1", request_fingerprint="fp-b")

    other_workspace, other_created = _submit(
        executor,
        work,
        workspace_id="workspace-2",
        idempotency_key="k1",
        request_fingerprint="fp-a",
    )
    other_tenant, _ = _submit(
        executor, work, tenant_id=OTHER, idempotency_key="k1", request_fingerprint="fp-a"
    )
    assert other_created and other_workspace.id != first.id and other_tenant.id != first.id


def test_admission_is_bounded_and_concurrency_never_exceeds_workers(make) -> None:
    _, executor = make(max_workers=2, max_queued=1)
    gate = threading.Event()
    guard = threading.Lock()
    running = {"now": 0, "peak": 0}

    def work(_context: JobContext) -> None:
        with guard:
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
        gate.wait(WAIT)
        with guard:
            running["now"] -= 1

    admitted = [_submit(executor, work)[0] for _ in range(3)]
    with pytest.raises(JobQueueFullError):
        _submit(executor, work)

    gate.set()
    for record in admitted:
        assert executor.wait(record.id, TENANT, WAIT).status is JobStatus.SUCCEEDED
    assert running["peak"] <= 2
    assert _submit(executor, lambda _context: None)[1], "capacity frees up after jobs finish"


def test_cancelling_a_queued_job_prevents_it_from_ever_running(make) -> None:
    _, executor = make(max_workers=1, max_queued=1)
    gate = threading.Event()
    ran: list[str] = []
    blocker, _ = _submit(executor, lambda _context: gate.wait(WAIT))
    queued, _ = _submit(executor, lambda _context: ran.append("queued"))

    cancelled = executor.cancel(queued.id, TENANT)
    gate.set()
    executor.wait(blocker.id, TENANT, WAIT)
    executor.wait(queued.id, TENANT, WAIT)

    assert cancelled.status is JobStatus.CANCELLED
    assert executor.get(queued.id, TENANT).status is JobStatus.CANCELLED
    assert ran == []
    assert executor.cancel(queued.id, TENANT).status is JobStatus.CANCELLED  # idempotent


def test_running_jobs_stop_cooperatively_or_finish_anyway(make) -> None:
    _, executor = make(max_workers=2, max_queued=0)
    started = threading.Event()
    stubborn_started = threading.Event()
    release = threading.Event()

    def polite(context: JobContext) -> None:
        started.set()
        while True:
            context.raise_if_cancelled()
            release.wait(0.01)

    def stubborn(_context: JobContext) -> str:
        stubborn_started.set()
        release.wait(WAIT)
        return "finished anyway"

    polite_job, _ = _submit(executor, polite)
    stubborn_job, _ = _submit(executor, stubborn)
    assert started.wait(WAIT) and stubborn_started.wait(WAIT)

    assert executor.cancel(polite_job.id, TENANT).cancel_requested
    executor.cancel(stubborn_job.id, TENANT)
    release.set()

    assert executor.wait(polite_job.id, TENANT, WAIT).status is JobStatus.CANCELLED
    stubborn_done = executor.wait(stubborn_job.id, TENANT, WAIT)
    assert stubborn_done.status is JobStatus.SUCCEEDED, "cancellation is best effort"
    assert executor.cancel(stubborn_job.id, TENANT).status is JobStatus.SUCCEEDED


def test_failures_expose_only_a_safe_category(make, events) -> None:
    _, executor = make()
    secret = "SELECT salary FROM payroll WHERE name = 'Alice' -- row data"

    def work(_context: JobContext) -> None:
        raise ValueError(secret)

    record, _ = _submit(executor, work)
    failed = executor.wait(record.id, TENANT, WAIT)

    assert failed.status is JobStatus.FAILED
    assert failed.error_category == "invalid_input"
    assert secret not in repr(failed)
    assert all(secret not in repr(event) for event in events)
    assert failed.result is None


def test_retry_runs_a_new_attempt_only_from_failed_within_the_budget(make) -> None:
    _, executor = make()

    def flaky(context: JobContext) -> str:
        if context.attempt == 1:
            raise RuntimeError("transient")
        return "recovered"

    record, _ = _submit(executor, flaky, max_attempts=2)
    assert executor.wait(record.id, TENANT, WAIT).status is JobStatus.FAILED

    retried = executor.retry(record.id, TENANT)
    assert retried.attempt == 2 and retried.status is JobStatus.QUEUED
    assert retried.error_category is None
    done = executor.wait(record.id, TENANT, WAIT)
    assert done.status is JobStatus.SUCCEEDED and done.result == "recovered"

    with pytest.raises(JobConflictError):
        executor.retry(record.id, TENANT)  # a succeeded job is never re-run


def test_retry_budget_is_exhausted_and_work_is_released(make) -> None:
    _, executor = make()

    def always_fails(_context: JobContext) -> None:
        raise RuntimeError("down")

    record, _ = _submit(executor, always_fails, max_attempts=2)
    executor.wait(record.id, TENANT, WAIT)
    executor.retry(record.id, TENANT)
    final = executor.wait(record.id, TENANT, WAIT)

    assert final.status is JobStatus.FAILED and final.attempt == 2 and not final.retryable
    with pytest.raises(JobConflictError):
        executor.retry(record.id, TENANT)


def test_expiry_clears_results_and_never_expires_running_jobs(make, clock) -> None:
    store, executor = make(max_workers=2, max_queued=0, ttl_minutes=10)
    gate = threading.Event()
    running, _ = _submit(executor, lambda _context: gate.wait(WAIT))
    done, _ = _submit(executor, lambda _context: "kept", tenant_id=OTHER)
    executor.wait(done.id, OTHER, WAIT)

    clock.advance(minutes=11)

    assert executor.get(running.id, TENANT).status is JobStatus.RUNNING
    expired = executor.get(done.id, OTHER)
    assert expired.status is JobStatus.EXPIRED and expired.result is None
    with pytest.raises(JobConflictError):
        executor.retry(done.id, OTHER)
    assert executor.purge_expired() == 1
    with pytest.raises(JobNotFoundError):
        executor.get(done.id, OTHER)
    gate.set()
    executor.wait(running.id, TENANT, WAIT)
    assert store.get(running.id, TENANT).status is JobStatus.EXPIRED


def test_purging_a_workspace_cancels_running_work_and_discards_its_outcome(make) -> None:
    _, executor = make(max_workers=1, max_queued=2)
    entered = threading.Event()
    release = threading.Event()
    observed: dict[str, bool] = {}

    def work(context: JobContext) -> str:
        entered.set()
        release.wait(WAIT)
        observed["cancel_seen"] = context.cancel_requested
        return "late result"

    running, _ = _submit(executor, work)
    queued, _ = _submit(executor, lambda _context: "never", workspace_id=WORKSPACE)
    other, _ = _submit(executor, lambda _context: "x", workspace_id="workspace-2")
    assert entered.wait(WAIT)

    assert executor.purge_workspace(TENANT, WORKSPACE) == 2
    release.set()
    with pytest.raises(JobNotFoundError):
        executor.get(running.id, TENANT)
    with pytest.raises(JobNotFoundError):
        executor.get(queued.id, TENANT)
    assert executor.wait(other.id, TENANT, WAIT).status is JobStatus.SUCCEEDED
    assert observed == {"cancel_seen": True}


def test_a_failing_observer_never_breaks_execution(clock) -> None:
    def broken(_event: JobEvent) -> None:
        raise RuntimeError("telemetry down")

    executor = InProcessJobExecutor(InMemoryJobStore(clock=clock), observer=broken)
    try:
        record, _ = _submit(executor, lambda _context: "fine")
        assert executor.wait(record.id, TENANT, WAIT).status is JobStatus.SUCCEEDED
    finally:
        executor.shutdown()


def test_handler_can_acknowledge_cancellation_explicitly(make) -> None:
    _, executor = make()

    def work(_context: JobContext) -> None:
        raise JobCancelledError

    record, _ = _submit(executor, work)
    assert executor.wait(record.id, TENANT, WAIT).status is JobStatus.CANCELLED


def test_duplicate_destructive_actions_run_once_and_cannot_be_retried(make) -> None:
    """A repeated workspace deletion (same idempotency key, or a retry) never executes twice."""
    _, executor = make()
    deletions: list[str] = []

    def delete_workspace(_context: JobContext) -> None:
        deletions.append(WORKSPACE)

    key = {"idempotency_key": "delete-1", "request_fingerprint": "workspace-1"}
    first, _ = _submit(executor, delete_workspace, operation="workspace.delete", **key)
    executor.wait(first.id, TENANT, WAIT)
    again, created = _submit(executor, delete_workspace, operation="workspace.delete", **key)

    assert not created and again.id == first.id
    with pytest.raises(JobConflictError):
        executor.retry(first.id, TENANT)
    assert deletions == [WORKSPACE]
