"""One behavioral contract for every ``DocumentIndex`` backend (Chroma always, pgvector in CI).

The pgvector cases need a real PostgreSQL with the ``vector`` extension. Set ``PGVECTOR_TEST_DSN``
to run them locally; CI runs them against a service container. Without it they are skipped with
the reason below, so a green local run never silently claims pgvector coverage.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from packages.evaluation.environment import EvaluationEnvironment
from packages.evaluation.fakes import HashingEmbedder
from packages.evaluation.fixtures import load_fixtures
from packages.evaluation.retrieval import run_retrieval_cases
from src.documents.chunker import DocumentChunk
from src.documents.pg_migrations import apply_migrations
from src.documents.pgvector_store import PgvectorDocumentStore, psycopg_connection_factory
from src.documents.retriever import DocumentRetriever
from src.documents.vector_store import ChromaDocumentStore
from tests.test_utils import isolated_vector_path

PG_DSN = os.getenv("PGVECTOR_TEST_DSN", "").strip()
SKIP_PG = pytest.mark.skipif(
    not PG_DSN,
    reason=(
        "PGVECTOR_TEST_DSN is not set; the pgvector contract runs in CI against a service "
        "container (see .github/workflows/ci.yml). No Postgres is needed for the Chroma cases."
    ),
)
EVALS = Path(__file__).resolve().parents[1] / "evals" / "v1"
StoreFactory = Callable[[str], Any]


def _chunk(chunk_id: str, text: str, filename: str = "policy.pdf", page: int = 1) -> DocumentChunk:
    return DocumentChunk(
        id=chunk_id,
        text=text,
        metadata={"filename": filename, "chunk_index": 0, "page_number": page},
    )


REFUND = _chunk("refund", "Refunds require receipts and manager approval before payment.")
SECURITY = _chunk("security", "Security incidents must be reported to the response team at once.")


@pytest.fixture(scope="module")
def pg_connect() -> Iterator[Callable[[], Any]]:
    connect = psycopg_connection_factory(PG_DSN)
    connection = connect()
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    yield connect


@pytest.fixture(params=["chroma", pytest.param("pgvector", marks=SKIP_PG)])
def make_store(request: pytest.FixtureRequest) -> Iterator[StoreFactory]:
    """Factory ``make_store(scope)``: each distinct scope is an isolated index of that backend."""
    created: list[Any] = []
    run_id = uuid4().hex[:8]

    directories: dict[str, Path] = {}

    def chroma(scope: str) -> Any:
        # The same scope must map to the same directory so a second store is a live reader.
        path = directories.setdefault(scope, isolated_vector_path(f"contract_{run_id}_{scope}"))
        store = ChromaDocumentStore(path, HashingEmbedder())
        created.append(store)
        return store

    def pgvector(scope: str) -> Any:
        connect = request.getfixturevalue("pg_connect")
        store = PgvectorDocumentStore(connect, f"contract/{run_id}-{scope}", HashingEmbedder())
        created.append(store)
        return store

    yield chroma if request.param == "chroma" else pgvector
    for store in created:
        try:
            store.purge() if hasattr(store, "purge") else store.reset()
        finally:
            store.close()


# --- contract ----------------------------------------------------------------------------------


def test_query_returns_the_relevant_chunk_with_metadata_and_a_score(
    make_store: StoreFactory,
) -> None:
    store = make_store("query")
    store.replace_chunks([REFUND, SECURITY])

    [top] = store.query("What is the refund policy?", top_k=1)

    assert "Refunds" in top.text
    assert top.metadata["filename"] == "policy.pdf" and top.metadata["page_number"] == 1
    assert top.distance is not None and top.relevance_score is not None
    assert 0.0 <= top.relevance_score <= 1.0
    assert store.count() == 2


def test_empty_questions_and_empty_indexes_return_nothing(make_store: StoreFactory) -> None:
    store = make_store("empty")

    assert store.query("anything") == [] and store.count() == 0
    store.replace_chunks([REFUND])
    assert store.query("   ") == []


def test_replace_swaps_in_new_content_completely(make_store: StoreFactory) -> None:
    store = make_store("swap")
    store.replace_chunks([REFUND, SECURITY])

    store.replace_chunks([_chunk("travel", "Travel expenses need a manager signature.")])

    assert store.count() == 1
    texts = [chunk.text for chunk in store.query("refund security travel expenses", top_k=5)]
    assert texts == ["Travel expenses need a manager signature."]


def test_embedding_failure_leaves_the_existing_index_untouched(make_store: StoreFactory) -> None:
    store = make_store("embedfail")
    store.replace_chunks([REFUND])

    class Exploding:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embedding backend down")

    original = store.embedder
    store.embedder = Exploding()
    with pytest.raises(RuntimeError):
        store.replace_chunks([SECURITY])
    store.embedder = original

    assert store.count() == 1 and "Refunds" in store.query("refund", top_k=1)[0].text


def test_storage_failure_part_way_leaves_the_existing_index_untouched(
    make_store: StoreFactory,
) -> None:
    store = make_store("storefail")
    store.replace_chunks([REFUND])

    duplicate_ids = [_chunk("same", "first copy"), _chunk("same", "second copy")]
    with pytest.raises(Exception):  # noqa: B017 - the exception type is backend-specific
        store.replace_chunks(duplicate_ids)

    assert store.count() == 1
    assert "Refunds" in store.query("refund", top_k=1)[0].text


def test_another_live_reader_sees_the_replacement(make_store: StoreFactory) -> None:
    writer, reader = make_store("live"), make_store("live")
    writer.replace_chunks([REFUND])
    assert "Refunds" in reader.query("refund", top_k=1)[0].text

    writer.replace_chunks([SECURITY])

    assert reader.count() == 1
    assert "Security" in reader.query("incident", top_k=1)[0].text


def test_readers_never_observe_a_partial_replacement(make_store: StoreFactory) -> None:
    store, reader = make_store("atomic"), make_store("atomic")
    store.replace_chunks([_chunk(f"old{i}", f"old policy text number {i}") for i in range(5)])
    replacement = [_chunk(f"new{i}", f"new policy text number {i}") for i in range(60)]
    seen: set[int] = set()
    done = threading.Event()

    def watch() -> None:
        while not done.is_set():
            seen.add(reader.count())

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        store.replace_chunks(replacement)
    finally:
        done.set()
        watcher.join()
    seen.add(reader.count())

    assert seen <= {5, 60}, f"observed an intermediate index size: {sorted(seen)}"
    assert 60 in seen


def test_scopes_are_isolated_for_reads_replacements_and_deletes(make_store: StoreFactory) -> None:
    tenant_a, tenant_b = make_store("tenant-a"), make_store("tenant-b")
    tenant_a.replace_chunks([REFUND])
    tenant_b.replace_chunks([SECURITY])

    assert [c.text for c in tenant_a.query("security incident", top_k=5)] == [REFUND.text]
    assert [c.text for c in tenant_b.query("refund receipts", top_k=5)] == [SECURITY.text]
    tenant_a.replace_chunks([_chunk("travel", "Travel expenses need approval.")])
    tenant_a.reset()
    assert tenant_a.count() == 0 and tenant_b.count() == 1
    assert "Security" in tenant_b.query("incident", top_k=1)[0].text


def test_filename_filter_restricts_dense_results(make_store: StoreFactory) -> None:
    store = make_store("filter")
    store.replace_chunks(
        [
            _chunk("a", "Refund policy for customers.", filename="a.pdf"),
            _chunk("b", "Refund policy for vendors.", filename="b.pdf"),
        ]
    )

    only_b = store.query("refund policy", top_k=5, where={"filename": "b.pdf"})

    assert [c.metadata["filename"] for c in only_b] == ["b.pdf"]
    assert store.query("refund policy", top_k=5, where={"filename": "none.pdf"}) == []


def test_distance_threshold_filters_dense_results(make_store: StoreFactory) -> None:
    store = make_store("distance")
    store.replace_chunks([REFUND])

    assert store.query("refund receipts approval", top_k=3, max_distance=2.0) != []
    assert store.query("zebra giraffe antelope", top_k=3, max_distance=0.01) == []


def test_exact_term_search_is_gated_filtered_and_never_stale(make_store: StoreFactory) -> None:
    store = make_store("lexical")
    store.replace_chunks(
        [
            _chunk("code", "Appendix. Exception code ZX-9141 applies to sandbox merchants only."),
            _chunk("other", "Boiler inspection happens every spring.", filename="b.pdf"),
        ]
    )

    [(hit, score)] = store.lexical_search("what is ZX-9141", 3)
    assert "ZX-9141" in hit.text and score > 0
    assert store.lexical_search("what is ZX-9141", 3, filename="b.pdf") == []
    assert store.lexical_search("the weather in paris today", 3) == []

    store.replace_chunks([_chunk("code", "Appendix. Exception code QQ-7000 applies.")])
    assert store.lexical_search("ZX-9141", 3) == []
    assert store.lexical_search("QQ-7000", 3)


def test_the_hybrid_retriever_works_on_every_backend(make_store: StoreFactory) -> None:
    store = make_store("hybrid")
    filler = [
        _chunk(f"f{i}", f"The exception review threshold item {i} is described here.")
        for i in range(14)
    ]
    store.replace_chunks(
        [*filler, _chunk("needle", "Facilities schedule. Exception code ZX-9141 is sandbox only.")]
    )

    result = DocumentRetriever(store, max_distance=None, default_top_k=3).retrieve(
        "What does exception code ZX-9141 mean?"
    )

    assert any("ZX-9141" in chunk.text for chunk in result.chunks)


def test_purge_removes_everything_for_the_scope(make_store: StoreFactory) -> None:
    store = make_store("purge")
    store.replace_chunks([REFUND, SECURITY])

    if hasattr(store, "purge"):
        store.purge()
    else:
        store.reset()

    assert store.count() == 0


# --- retrieval evaluation parity (pgvector only; Chroma is covered by the evaluation gate) -----


@SKIP_PG
def test_pgvector_meets_the_committed_retrieval_thresholds(
    pg_connect: Callable[[], Any], tmp_path: Path
) -> None:
    fixtures = load_fixtures(EVALS)
    run_id = uuid4().hex[:8]
    stores: list[PgvectorDocumentStore] = []

    def factory(name: str, embedder: Any) -> PgvectorDocumentStore:
        store = PgvectorDocumentStore(pg_connect, f"eval/{run_id}-{name}", embedder)
        stores.append(store)
        return store

    environment = EvaluationEnvironment(fixtures, tmp_path, store_factory=factory)
    try:
        results, metrics = run_retrieval_cases(fixtures, environment)
    finally:
        environment.close()
        for store in stores:
            store.purge()

    minimums = fixtures.thresholds["retrieval_metrics_min"]
    assert all(result.passed for result in results), [r.id for r in results if not r.passed]
    for metric, minimum in minimums.items():
        assert metrics[metric] >= minimum, metric
    assert metrics["dense_control_recall_at_k"] < metrics["recall_at_k"]
