"""pgvector adapter, migrations, and backend selection, offline, with a recording fake connection.

These tests pin the properties that do not need a database: bound parameters, scope on every
statement, transaction boundaries, and fail-closed schema checks. Real-database behavior is covered
by ``test_document_index_contract.py`` when ``PGVECTOR_TEST_DSN`` is set (CI runs it against a
service container).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from reportlab.pdfgen import canvas

from config.settings import Settings
from packages.evaluation.fakes import HashingEmbedder
from packages.retrieval import DocumentApplicationService, IndexDocumentsCommand
from scripts import migrate_postgres
from src.documents.chunker import DocumentChunk
from src.documents.pg_migrations import (
    LATEST_VERSION,
    MIGRATION_LOCK_KEY,
    SchemaError,
    apply_migrations,
    verify_schema,
)
from src.documents.pgvector_store import PgvectorDocumentStore, psycopg_connection_factory
from src.documents.retriever import DocumentRetriever
from src.documents.vector_store import ChromaDocumentStore
from tests.test_api_features import _settings
from tests.test_utils import isolated_vector_path

SCOPE = "tenant123/ws_abc"
INJECTION = "x'); DROP TABLE document_chunks; --"


class FakeCursor:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.rows: list[Any] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.db.log.append((" ".join(sql.split()), tuple(params)))
        if self.db.fail_when is not None and self.db.fail_when(sql, params):
            raise RuntimeError("boom: connection reset")
        self.rows = list(self.db.responder(sql, params))

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return self.rows


class FakeConnection:
    def __init__(self, db: FakeDb) -> None:
        self.db = db

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db)

    def commit(self) -> None:
        self.db.events.append("commit")

    def rollback(self) -> None:
        self.db.events.append("rollback")

    def close(self) -> None:
        self.db.events.append("close")


class FakeDb:
    def __init__(self, responder: Callable[[str, tuple], list[Any]] | None = None) -> None:
        self.log: list[tuple[str, tuple]] = []
        self.events: list[str] = []
        self.connections = 0
        self.fail_when: Callable[[str, tuple], bool] | None = None
        self.responder = responder or self._default

    @staticmethod
    def _default(sql: str, params: tuple) -> list[Any]:
        if "to_regclass" in sql:
            return [("schema_migrations",)]
        if "MAX(version)" in sql:
            return [(LATEST_VERSION,)]
        if "string_agg" in sql:
            return [(0, "")]  # aggregates always return one row
        if sql.startswith("SELECT count(*)"):
            return [(0,)]
        return []

    def connect(self) -> FakeConnection:
        self.connections += 1
        return FakeConnection(self)

    def since(self, mark: int) -> list[tuple[str, tuple]]:
        return self.log[mark:]


def _store(db: FakeDb | None = None, scope: str = SCOPE, **kwargs: Any) -> tuple[Any, FakeDb]:
    db = db or FakeDb()
    return PgvectorDocumentStore(db.connect, scope, HashingEmbedder(), **kwargs), db


def _chunks(*texts: str) -> list[DocumentChunk]:
    return [
        DocumentChunk(id=f"c{i}", text=text, metadata={"filename": "p.pdf", "chunk_index": i})
        for i, text in enumerate(texts)
    ]


# --- construction and scope --------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["", "a b", INJECTION, "x" * 201, "tenant\n/ws"])
def test_unsafe_scopes_are_rejected_before_any_connection(scope: str) -> None:
    db = FakeDb()

    with pytest.raises(ValueError):
        PgvectorDocumentStore(db.connect, scope, HashingEmbedder())

    assert db.connections == 0


def test_startup_verifies_the_schema_and_fails_closed_when_it_is_wrong() -> None:
    behind = FakeDb(
        lambda sql, params: [(0,)] if "MAX(version)" in sql else [("schema_migrations",)]
    )
    ahead = FakeDb(
        lambda sql, params: [(LATEST_VERSION + 1,)] if "MAX(version)" in sql else [("x",)]
    )
    missing = FakeDb(lambda sql, params: [(None,)])

    for db, message in (
        (behind, "migrate_postgres"),
        (ahead, "newer"),
        (missing, "migrate_postgres"),
    ):
        with pytest.raises(SchemaError, match=message):
            PgvectorDocumentStore(db.connect, SCOPE, HashingEmbedder())
        assert "commit" not in db.events  # verification never changes the database


# --- writes ------------------------------------------------------------------------------------


def test_replace_is_one_transaction_deleting_the_scope_then_inserting_every_chunk() -> None:
    store, db = _store()
    mark = len(db.log)
    db.events.clear()

    store.replace_chunks(_chunks("alpha policy", "beta policy"))

    statements = db.since(mark)
    assert [sql.split()[0] for sql, _ in statements] == ["DELETE", "INSERT", "INSERT"]
    assert statements[0][1] == (SCOPE,)
    assert all(params[0] == SCOPE for _, params in statements[1:])
    assert db.events == ["commit", "close"]  # one commit, no rollback


def test_user_content_only_ever_travels_as_bound_parameters() -> None:
    store, db = _store()
    mark = len(db.log)
    hostile = _chunks(INJECTION, "normal")
    hostile[0].metadata["filename"] = INJECTION

    store.replace_chunks(hostile)
    store.query(INJECTION, top_k=2, where={"filename": INJECTION})

    for sql, params in db.since(mark):
        assert "DROP TABLE" not in sql and "document_chunks;" not in sql
        assert sql.count("%s") == len(params)
    flat = json.dumps([params for _, params in db.since(mark)], default=str)
    assert INJECTION.replace("'", "'") in flat or "DROP TABLE" in flat  # present, but as data


def test_embedding_failure_never_touches_the_database() -> None:
    class FailingEmbedder:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embedding backend down")

    db = FakeDb()
    store = PgvectorDocumentStore(db.connect, SCOPE, FailingEmbedder())
    connections, mark = db.connections, len(db.log)

    with pytest.raises(RuntimeError):
        store.replace_chunks(_chunks("a"))

    assert db.connections == connections and db.since(mark) == []


def test_a_failure_part_way_rolls_back_so_the_previous_index_survives() -> None:
    store, db = _store()
    db.events.clear()
    db.fail_when = lambda sql, params: sql.lstrip().startswith("INSERT") and params[1] == "c1"

    with pytest.raises(RuntimeError, match="boom"):
        store.replace_chunks(_chunks("one", "two", "three"))

    assert db.events == ["rollback", "close"] and "commit" not in db.events


def test_reset_and_purge_delete_only_this_scope() -> None:
    store, db = _store()
    mark = len(db.log)

    store.reset()
    store.purge()

    assert [(sql, params) for sql, params in db.since(mark)] == [
        ("DELETE FROM document_chunks WHERE scope = %s", (SCOPE,))
    ] * 2


def test_add_chunks_upserts_within_the_scope() -> None:
    store, db = _store()
    mark = len(db.log)

    store.add_chunks(_chunks("a"))
    store.add_chunks([])

    [(sql, params)] = db.since(mark)
    assert "ON CONFLICT (scope, chunk_id) DO UPDATE" in sql and params[0] == SCOPE


# --- reads -------------------------------------------------------------------------------------


def test_query_binds_scope_vector_and_a_clamped_limit() -> None:
    store, db = _store()
    mark = len(db.log)

    store.query("refund policy", top_k=10**6)
    store.query("refund policy", top_k=0)
    store.query("refund policy", top_k=3, where={"filename": "p.pdf"})
    store.query("   ")

    first, second, third = db.since(mark)
    assert first[1][1] == SCOPE and first[1][2] == 200
    assert second[1][2] == 1
    assert "metadata ->> 'filename' = %s" in third[0] and third[1][1:] == (SCOPE, "p.pdf", 3)
    assert first[1][0].startswith("[") and first[1][0].endswith("]")


def test_query_converts_rows_and_applies_the_distance_threshold() -> None:
    def rows(sql: str, params: tuple) -> list[Any]:
        if "<=>" in sql:
            return [
                ("near", {"filename": "p.pdf"}, 0.1),
                ("far", '{"filename": "p.pdf"}', 0.9),
                ("odd", {"filename": "p.pdf"}, 1.7),
            ]
        return FakeDb._default(sql, params)

    store, _db = _store(FakeDb(rows))

    chunks = store.query("q", top_k=3, max_distance=0.95)

    assert [c.text for c in chunks] == ["near", "far"]  # 1.7 exceeds the 0.95 threshold
    assert chunks[0].relevance_score == pytest.approx(0.9)
    assert [c.text for c in store.query("q", top_k=3, max_distance=0.5)] == ["near"]
    unbounded = store.query("q", top_k=3)
    assert unbounded[2].relevance_score == 0.0  # clamped, never negative


def test_every_statement_after_startup_is_scoped() -> None:
    store, db = _store()
    mark = len(db.log)

    store.replace_chunks(_chunks("alpha"))
    store.add_chunks(_chunks("beta"))
    store.query("alpha")
    store.lexical_search("alpha", 3)
    store.count()
    store.reset()

    for sql, params in db.since(mark):
        assert "scope" in sql and SCOPE in params, sql


def test_lexical_index_is_rebuilt_only_when_the_fingerprint_changes() -> None:
    state = {"digest": "aaa"}

    def rows(sql: str, params: tuple) -> list[Any]:
        if "string_agg" in sql:
            return [(2, state["digest"])]
        if sql.startswith("SELECT content, metadata FROM document_chunks"):
            return [
                ("error code ZX-9141 applies here", {"filename": "p.pdf"}),
                ("unrelated boiler text", {"filename": "p.pdf"}),
            ]
        return FakeDb._default(sql, params)

    store, db = _store(FakeDb(rows))
    selects = lambda: sum(  # noqa: E731
        1 for sql, _ in db.log if sql.startswith("SELECT content, metadata FROM document_chunks")
    )

    first = store.lexical_search("what is ZX-9141", 3)
    store.lexical_search("what is ZX-9141", 3)
    assert selects() == 1 and "ZX-9141" in first[0][0].text
    state["digest"] = "bbb"
    store.lexical_search("what is ZX-9141", 3)
    assert selects() == 2
    assert store.lexical_search("what is ZX-9141", 3, filename="other.pdf") == []


def test_every_operation_closes_its_connection() -> None:
    store, db = _store()
    db.events.clear()

    store.query("q")
    store.count()
    store.replace_chunks(_chunks("a"))

    assert db.events.count("close") == 3


# --- migrations --------------------------------------------------------------------------------


class MigrationDb:
    """Minimal stateful stand-in for the version table, with transactional commit/rollback."""

    def __init__(self, applied: tuple[int, ...] = (), has_table: bool = False) -> None:
        self.committed = set(applied)
        self.pending: set[int] = set()
        self.has_table = has_table
        self.statements: list[str] = []
        self.fail_on: str | None = None
        self.rolled_back = 0

    def connect(self) -> Any:
        return self

    def cursor(self) -> Any:
        return self

    def __enter__(self) -> MigrationDb:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        text = " ".join(sql.split())
        self.statements.append(text)
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("migration statement failed")
        self._result: list[Any] = []
        if "to_regclass" in text:
            self._result = [("schema_migrations" if self.has_table else None,)]
        elif "MAX(version)" in text:
            self._result = [(max(self.committed | self.pending, default=0),)]
        elif text.startswith("CREATE TABLE IF NOT EXISTS schema_migrations"):
            self.has_table = True
        elif text.startswith("INSERT INTO schema_migrations"):
            self.pending.add(params[0])

    def fetchone(self) -> Any:
        return self._result[0]

    def commit(self) -> None:
        self.committed |= self.pending
        self.pending = set()

    def rollback(self) -> None:
        self.rolled_back += 1
        self.pending = set()

    def close(self) -> None:
        return None


def test_apply_takes_the_advisory_lock_first_and_is_idempotent() -> None:
    db = MigrationDb()

    first = apply_migrations(db)
    second = apply_migrations(db)

    assert first == [1] and second == []
    assert db.statements[0] == "SELECT pg_advisory_xact_lock(%s)"
    assert db.committed == {1}
    assert verify_schema(db) == LATEST_VERSION
    assert MIGRATION_LOCK_KEY > 0


def test_a_failing_migration_rolls_back_and_records_nothing() -> None:
    db = MigrationDb()
    db.fail_on = "CREATE TABLE document_chunks"

    with pytest.raises(RuntimeError):
        apply_migrations(db)

    assert db.committed == set() and db.rolled_back == 1


def test_apply_refuses_to_touch_a_database_newer_than_the_code() -> None:
    db = MigrationDb(applied=(LATEST_VERSION + 1,), has_table=True)

    with pytest.raises(SchemaError, match="refusing"):
        apply_migrations(db)

    assert db.committed == {LATEST_VERSION + 1}


def test_verify_reports_missing_behind_and_ahead_schemas() -> None:
    with pytest.raises(SchemaError, match="migrate_postgres"):
        verify_schema(MigrationDb())
    with pytest.raises(SchemaError, match="newer"):
        verify_schema(MigrationDb(applied=(LATEST_VERSION + 1,), has_table=True))
    assert verify_schema(MigrationDb(applied=(LATEST_VERSION,), has_table=True)) == LATEST_VERSION


def test_auto_migrate_creates_the_schema_before_verifying() -> None:
    db = MigrationDb()

    PgvectorDocumentStore(db.connect, SCOPE, HashingEmbedder(), auto_migrate=True)

    assert db.committed == {1}


# --- migration script --------------------------------------------------------------------------


def test_script_requires_a_dsn_and_never_echoes_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert migrate_postgres.main([], environ={}) == 2

    def leaky() -> Any:
        raise RuntimeError("could not connect to postgresql://user:SECRETPASS@db/bi")

    code = migrate_postgres.main(
        [], environ={"POSTGRES_DSN": "postgresql://u:SECRETPASS@h/d"}, connect=leaky
    )

    captured = capsys.readouterr()
    assert code == 1 and "SECRETPASS" not in captured.out + captured.err
    assert "RuntimeError" in captured.err


def test_script_applies_and_verifies_with_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    db = MigrationDb()

    assert migrate_postgres.main(["--verify"], connect=db.connect) == 1
    assert migrate_postgres.main([], connect=db.connect) == 0
    assert migrate_postgres.main(["--verify"], connect=db.connect) == 0
    assert "version 1" in capsys.readouterr().out


# --- settings and wiring -----------------------------------------------------------------------


def _with(tmp_path: Path, **overrides: Any) -> Settings:
    return replace(_settings(tmp_path), **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"document_index_backend": "mysql"},
        {"document_index_backend": "pgvector"},
        {"document_index_backend": "pgvector", "postgres_dsn": "mysql://u:p@h/d"},
        {"document_index_backend": "pgvector", "postgres_dsn": "   "},
    ],
)
def test_pgvector_without_environment_credentials_fails_closed(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        _with(tmp_path, **overrides).validate_document_index_configuration()


def test_defaults_stay_on_chroma_and_the_dsn_is_never_in_the_repr(tmp_path: Path) -> None:
    default = _with(tmp_path)
    default.validate_document_index_configuration()
    pg = _with(
        tmp_path, document_index_backend="pgvector", postgres_dsn="postgresql://u:SECRETPASS@h/d"
    )

    pg.validate_document_index_configuration()

    assert default.document_index_backend == "chroma" and default.postgres_auto_migrate is False
    assert "SECRETPASS" not in repr(pg)


def test_environment_selects_the_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from config.settings import get_settings

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DOCUMENT_INDEX_BACKEND", "PGVECTOR")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@127.0.0.1:5432/bi")
    monkeypatch.setenv("POSTGRES_AUTO_MIGRATE", "true")

    settings = get_settings()

    assert settings.document_index_backend == "pgvector" and settings.postgres_auto_migrate is True
    monkeypatch.delenv("POSTGRES_DSN")
    with pytest.raises(ValueError):
        get_settings()


def _pdf() -> bytes:
    buffer = BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 720, "Policy: Transactions over 50 require escalation.")
    document.save()
    return buffer.getvalue()


def _command(tmp_path: Path, **overrides: Any) -> IndexDocumentsCommand:
    from packages.retrieval import DocumentPayload

    base = {
        "documents": (DocumentPayload("policy.pdf", _pdf()),),
        "persist_dir": isolated_vector_path("pg_wiring"),
        "embedding_model": "hashing",
        "max_pages": 10,
        "max_chunks": 100,
        "retrieval_top_k": 4,
        "retrieval_max_distance": None,
    }
    return IndexDocumentsCommand(**{**base, **overrides})


def test_the_service_builds_the_selected_backend() -> None:
    service = DocumentApplicationService(lambda _model: HashingEmbedder())
    db = FakeDb()

    pg = service.index(
        _command(
            Path("."),
            index_backend="pgvector",
            index_scope=SCOPE,
            pgvector_connect=db.connect,
        )
    )
    chroma = service.index(_command(Path(".")))

    try:
        assert isinstance(pg.retriever.store, PgvectorDocumentStore)
        assert pg.retriever.store.scope == SCOPE
        assert any(sql.startswith("INSERT") for sql, _ in db.log)
        assert isinstance(chroma.retriever.store, ChromaDocumentStore)
    finally:
        chroma.retriever.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"index_backend": "pgvector"},
        {"index_backend": "pgvector", "index_scope": SCOPE},
        {"index_backend": "pgvector", "pgvector_connect": FakeDb().connect},
        {"index_backend": "cassandra"},
    ],
)
def test_misconfigured_backends_fail_before_touching_anything(overrides: dict[str, Any]) -> None:
    service = DocumentApplicationService(lambda _model: HashingEmbedder())

    with pytest.raises(ValueError):
        service.index(_command(Path("."), **overrides))


def test_the_command_repr_does_not_expose_the_connection_factory() -> None:
    secret_factory = psycopg_connection_factory("postgresql://u:SECRETPASS@h/d")

    command = _command(Path("."), index_backend="pgvector", pgvector_connect=secret_factory)

    assert "SECRETPASS" not in repr(command) and "pgvector_connect" not in repr(command)


def test_purge_removes_database_rows_and_is_a_no_op_for_directory_stores() -> None:
    store, db = _store()
    mark = len(db.log)

    DocumentRetriever(store).purge()

    assert db.since(mark) == [("DELETE FROM document_chunks WHERE scope = %s", (SCOPE,))]
    chroma = DocumentRetriever(
        ChromaDocumentStore(isolated_vector_path("pg_purge_noop"), HashingEmbedder())
    )
    chroma.purge()  # nothing to do: the directory goes with the workspace
    chroma.close()
