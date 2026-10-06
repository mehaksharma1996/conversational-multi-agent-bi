"""pgvector-backed document index implementing the same contract as ``ChromaDocumentStore``.

Isolation: every row carries a ``scope`` (``<tenant>/<workspace>``) and every statement filters on
it with a bound parameter, so one scope can never read, replace, or delete another's rows. The
scope is supplied by server code, never by a caller.

Atomic replacement: ``replace_chunks`` embeds everything *before* opening a database transaction
(the external, failure-prone step), then deletes the scope's rows and inserts the new ones in one
transaction. PostgreSQL's MVCC means concurrent readers keep seeing the previous index until
commit, and a failure rolls back to it, so there is no partial or missing index and no staging
collection to clean up.

SQL safety: statements are constants; chunk text, questions, filenames, and scopes only ever
travel as bound parameters. This module is about the *document index*; guarded user-data SQL
stays on SQLite (ADR 0018).

``psycopg`` is imported lazily by the default connection factory so installing the application
without the optional Postgres requirements keeps working.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable
from contextlib import closing
from threading import RLock
from typing import Any

from src.documents.chunker import DocumentChunk
from src.documents.embedding import TextEmbedder
from src.documents.lexical import BM25Index
from src.documents.pg_migrations import apply_migrations, verify_schema
from src.documents.vector_store import (
    DEFAULT_EMBEDDING_BATCH_SIZE,
    RetrievedChunk,
    metadata_matches,
)

ConnectionFactory = Callable[[], Any]
MAX_QUERY_RESULTS = 200
_SCOPE = re.compile(r"[A-Za-z0-9_./:-]{1,200}")

_DELETE_SCOPE = "DELETE FROM document_chunks WHERE scope = %s"
_INSERT_CHUNK = (
    "INSERT INTO document_chunks (scope, chunk_id, position, content, metadata, embedding) "
    "VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector)"
)
_UPSERT_CHUNK = (
    _INSERT_CHUNK + " ON CONFLICT (scope, chunk_id) DO UPDATE SET position = EXCLUDED.position, "
    "content = EXCLUDED.content, metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding"
)
_COUNT_SCOPE = "SELECT count(*) FROM document_chunks WHERE scope = %s"
_SELECT_NEAREST = (
    "SELECT content, metadata, embedding <=> %s::vector AS distance FROM document_chunks "
    "WHERE scope = %s ORDER BY distance LIMIT %s"
)
_SELECT_NEAREST_IN_FILE = (
    "SELECT content, metadata, embedding <=> %s::vector AS distance FROM document_chunks "
    "WHERE scope = %s AND metadata ->> 'filename' = %s ORDER BY distance LIMIT %s"
)
_SELECT_FINGERPRINT = (
    "SELECT count(*), "
    "COALESCE(md5(string_agg(chunk_id || md5(content), ',' ORDER BY chunk_id)), '') "
    "FROM document_chunks WHERE scope = %s"
)
_SELECT_ALL = (
    "SELECT content, metadata FROM document_chunks WHERE scope = %s ORDER BY position, chunk_id"
)


def psycopg_connection_factory(dsn: str) -> ConnectionFactory:
    """Connections from a DSN held only in memory; it is never logged or kept on the store."""

    def connect() -> Any:
        psycopg = importlib.import_module("psycopg")
        return psycopg.connect(dsn, autocommit=False)

    return connect


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in embedding) + "]"


class PgvectorDocumentStore:
    """Persist and query one scope's document chunks in PostgreSQL with pgvector."""

    def __init__(
        self,
        connect: ConnectionFactory,
        scope: str,
        embedder: TextEmbedder,
        *,
        auto_migrate: bool = False,
    ) -> None:
        if _SCOPE.fullmatch(scope) is None:
            raise ValueError("The index scope is not a safe identifier.")
        self._connect = connect
        self.scope = scope
        self.embedder = embedder
        self._lock = RLock()
        self._lexical_cache: tuple[tuple[int, str], BM25Index, list[tuple[str, dict]]] | None = None
        with closing(self._connect()) as connection:
            if auto_migrate:
                apply_migrations(connection)
            verify_schema(connection)
            connection.rollback()

    # -- writes ----------------------------------------------------------------------------------

    def reset(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            self._run(connection, [(_DELETE_SCOPE, (self.scope,))])

    def purge(self) -> None:
        """Remove every row for this scope (used when its workspace is deleted)."""
        self.reset()

    def add_chunks(
        self, chunks: list[DocumentChunk], batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    ) -> None:
        if not chunks:
            return
        with self._lock:
            embeddings = self._embed(chunks, batch_size)
            with closing(self._connect()) as connection:
                self._run(connection, self._rows(_UPSERT_CHUNK, chunks, embeddings))

    def replace_chunks(
        self, chunks: list[DocumentChunk], batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    ) -> None:
        """Atomically replace this scope's contents; a failure leaves the previous index intact."""
        with self._lock:
            embeddings = self._embed(chunks, batch_size)  # before any database work
            statements = [(_DELETE_SCOPE, (self.scope,))]
            statements.extend(self._rows(_INSERT_CHUNK, chunks, embeddings))
            with closing(self._connect()) as connection:
                self._run(connection, statements)

    # -- reads -----------------------------------------------------------------------------------

    def query(
        self,
        question: str,
        top_k: int = 4,
        max_distance: float | None = None,
        where: dict | None = None,
    ) -> list[RetrievedChunk]:
        if not question.strip():
            return []
        limit = max(1, min(int(top_k), MAX_QUERY_RESULTS))
        filename = (where or {}).get("filename")
        with self._lock:
            vector = _vector_literal(self.embedder.embed_texts([question])[0])
            params: tuple
            if filename is not None:
                sql, params = (
                    _SELECT_NEAREST_IN_FILE,
                    (vector, self.scope, str(filename), limit),
                )
            else:
                sql, params = _SELECT_NEAREST, (vector, self.scope, limit)
            with closing(self._connect()) as connection:
                rows = self._fetch(connection, sql, params)
        chunks: list[RetrievedChunk] = []
        for content, metadata, distance in rows:
            numeric = float(distance) if distance is not None else None
            if max_distance is not None and numeric is not None and numeric > max_distance:
                continue
            relevance = max(0.0, min(1.0, 1.0 - numeric)) if numeric is not None else None
            chunks.append(
                RetrievedChunk(
                    text=str(content),
                    metadata=metadata if isinstance(metadata, dict) else json.loads(metadata),
                    distance=numeric,
                    relevance_score=relevance,
                )
            )
        return chunks

    def lexical_search(
        self,
        question: str,
        limit: int,
        *,
        filename: str | None = None,
        page_min: int | None = None,
        page_max: int | None = None,
    ) -> list[tuple[RetrievedChunk, float]]:
        """Gated BM25 hits over the scope's *current* rows (rebuilt when content changes)."""
        if not question.strip():
            return []
        with self._lock, closing(self._connect()) as connection:
            count, digest = self._fetch(connection, _SELECT_FINGERPRINT, (self.scope,))[0]
            if int(count) == 0:
                return []
            key = (int(count), str(digest))
            if self._lexical_cache is None or self._lexical_cache[0] != key:
                rows = self._fetch(connection, _SELECT_ALL, (self.scope,))
                texts = [str(content) for content, _ in rows]
                metadatas = [
                    metadata if isinstance(metadata, dict) else json.loads(metadata)
                    for _, metadata in rows
                ]
                self._lexical_cache = (
                    key,
                    BM25Index(texts),
                    list(zip(texts, metadatas, strict=True)),
                )
            _, index, entries = self._lexical_cache
        allowed = {
            position
            for position, (_, metadata) in enumerate(entries)
            if metadata_matches(metadata, filename, page_min, page_max)
        }
        hits = index.search(question, limit, allowed if len(allowed) != len(entries) else None)
        return [
            (
                RetrievedChunk(
                    text=entries[hit.index][0],
                    metadata=entries[hit.index][1],
                    distance=None,
                    relevance_score=hit.score / (hit.score + 4.0),
                ),
                hit.score,
            )
            for hit in hits
        ]

    def count(self) -> int:
        with self._lock, closing(self._connect()) as connection:
            return int(self._fetch(connection, _COUNT_SCOPE, (self.scope,))[0][0])

    def close(self) -> None:
        """Connections are opened per operation, so there is nothing to release."""
        self._lexical_cache = None

    # -- internals -------------------------------------------------------------------------------

    def _embed(self, chunks: list[DocumentChunk], batch_size: int) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for start in range(0, len(chunks), max(1, batch_size)):
            batch = chunks[start : start + max(1, batch_size)]
            embeddings.extend(self.embedder.embed_texts([chunk.text for chunk in batch]))
        return embeddings

    def _rows(
        self, sql: str, chunks: list[DocumentChunk], embeddings: list[list[float]]
    ) -> list[tuple[str, tuple]]:
        return [
            (
                sql,
                (
                    self.scope,
                    chunk.id,
                    position,
                    chunk.text,
                    json.dumps(chunk.metadata, sort_keys=True),
                    _vector_literal(embedding),
                ),
            )
            for position, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True))
        ]

    @staticmethod
    def _run(connection: Any, statements: list[tuple[str, tuple]]) -> None:
        """Execute statements in one transaction: all commit together or none do."""
        try:
            with connection.cursor() as cursor:
                for sql, params in statements:
                    cursor.execute(sql, params)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _fetch(connection: Any, sql: str, params: tuple) -> list[Any]:
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
            connection.rollback()  # read-only: end the implicit transaction
            return list(rows)
        except BaseException:
            connection.rollback()
            raise
