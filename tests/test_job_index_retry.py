"""A failed or retried index job never publishes a partial index (issue #10, ADR 0020 invariant)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.repository import LocalResourceRepository, ResourceNotFoundError
from packages.evaluation.fakes import HashingEmbedder
from packages.jobs import (
    InMemoryJobStore,
    InProcessJobExecutor,
    JobConflictError,
    JobContext,
    JobStatus,
)
from packages.retrieval.service import (
    DocumentApplicationService,
    DocumentPayload,
    IndexDocumentsCommand,
)
from scripts.run_benchmarks import synthetic_pdf
from src.documents.embedding import EmbeddingError
from src.documents.vector_store import DEFAULT_EMBEDDING_BATCH_SIZE, ChromaDocumentStore
from tests.test_utils import isolated_directory_path, isolated_vector_path

TENANT = "tenant-a"
WORKSPACE = "workspace-1"
WAIT = 120.0  # an upper bound only: shared Windows runners can be slow under load


class FlakyEmbedder:
    """Embeds the first batch, then fails while armed (a provider outage mid-index)."""

    def __init__(self) -> None:
        self.inner = HashingEmbedder()
        self.armed = False
        self.calls = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.armed and self.calls >= 2:
            raise EmbeddingError("embedding provider unavailable")
        return self.inner.embed_texts(texts)


def _command(persist_dir: Path, pdf: bytes, name: str) -> IndexDocumentsCommand:
    return IndexDocumentsCommand(
        documents=(DocumentPayload(name, pdf),),
        persist_dir=persist_dir,
        embedding_model="test",
        max_pages=10_000,
        max_chunks=100_000,
        retrieval_top_k=4,
        retrieval_max_distance=None,
    )


def _live_state(persist_dir: Path) -> tuple[int, set[str], str]:
    """Open the index independently, as a reader would, and describe what it publishes."""
    store = ChromaDocumentStore(persist_dir, HashingEmbedder())
    try:
        names = {collection.name for collection in store._client.list_collections()}
        hits = store.query("manager approval threshold", top_k=1)
        filename = str(hits[0].metadata.get("filename")) if hits else ""
        return store.count(), names, filename
    finally:
        store.close()


def test_failed_index_job_keeps_the_previous_index_and_retry_replaces_it_once() -> None:
    persist_dir = isolated_vector_path("job_index_retry")
    embedder = FlakyEmbedder()
    service = DocumentApplicationService(lambda _model: embedder)
    first_pdf = synthetic_pdf(5, seed=1)
    second_pdf = synthetic_pdf(60, seed=2)

    previous = service.index(_command(persist_dir, first_pdf, "first.pdf"))
    previous_chunks = previous.chunk_count
    previous.retriever.close()
    new_chunks = None

    embedder.armed = True
    embedder.calls = 0
    executor = InProcessJobExecutor(InMemoryJobStore(), max_workers=1, max_queued=0)

    def index_job(context: JobContext) -> int:
        context.progress("indexing", 0, 1)
        result = service.index(_command(persist_dir, second_pdf, "second.pdf"))
        result.retriever.close()
        context.progress("indexing", 1, 1)
        return result.chunk_count

    try:
        record, _ = executor.submit(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            operation="documents.index",
            work=index_job,
            idempotency_key="index-1",
            request_fingerprint="second.pdf",
        )
        failed = executor.wait(record.id, TENANT, WAIT)

        assert failed.status is JobStatus.FAILED and failed.error_category == "dependency"
        assert embedder.calls >= 2, "the failure must happen after a batch was already embedded"
        count, names, filename = _live_state(persist_dir)
        assert count == previous_chunks, "no partial replacement was published"
        assert filename == "first.pdf"
        assert names == {"uploaded_documents"}, "no staging collection is left behind"

        embedder.armed = False
        executor.retry(record.id, TENANT)
        succeeded = executor.wait(record.id, TENANT, WAIT)
        assert succeeded.status is JobStatus.SUCCEEDED and succeeded.attempt == 2
        new_chunks = succeeded.result
    finally:
        executor.shutdown()

    assert isinstance(new_chunks, int) and new_chunks > DEFAULT_EMBEDDING_BATCH_SIZE
    count, names, filename = _live_state(persist_dir)
    assert count == new_chunks, "the retry replaced the index exactly once (no duplicates)"
    assert filename == "second.pdf"
    assert names == {"uploaded_documents"}


def test_workspace_deletion_job_runs_once_even_when_replayed_or_retried() -> None:
    repository = LocalResourceRepository(storage_root=isolated_directory_path("job_delete"))
    deletions: list[str] = []
    repository.lifecycle_listener = lambda event, workspace, _reason: deletions.append(event)
    workspace = repository.create_workspace(
        tenant_id=TENANT, authentication_mode="local", idempotency_key=None
    )
    workspace_dir = repository.workspace_dir(workspace.id, TENANT)
    executor = InProcessJobExecutor(InMemoryJobStore(), max_workers=1, max_queued=0)

    def delete(_context: JobContext) -> None:
        repository.delete_workspace(workspace.id, TENANT)

    def submit_delete(key: str):
        return executor.submit(
            tenant_id=TENANT,
            workspace_id=workspace.id,
            operation="workspace.delete",
            work=delete,
            idempotency_key=key,
            request_fingerprint=workspace.id,
        )

    try:
        record, created = submit_delete("delete-1")
        assert created
        assert executor.wait(record.id, TENANT, WAIT).status is JobStatus.SUCCEEDED
        replay, replay_created = submit_delete("delete-1")
        assert not replay_created and replay.id == record.id

        with pytest.raises(JobConflictError):
            executor.retry(record.id, TENANT)  # a succeeded deletion is never retried

        # A *different* request for the same, already-deleted workspace cannot delete again: the
        # repository refuses, the job fails safely, and nothing else is removed or audited.
        second, _ = submit_delete("delete-2")
        failed = executor.wait(second.id, TENANT, WAIT)
        assert failed.status is JobStatus.FAILED and failed.error_category == "not_found"
    finally:
        executor.shutdown()

    assert deletions.count("deleted") == 1, "the destructive lifecycle action happened exactly once"
    assert not workspace_dir.exists()
    with pytest.raises(ResourceNotFoundError):
        repository.get_workspace(workspace.id, TENANT)
