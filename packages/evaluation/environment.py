"""Build real SQLite tables and Chroma indexes from synthetic fixtures.

The evaluation exercises the production guard, retrieval, and orchestration
code. Only the model and the embedding function are substituted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from packages.evaluation.fakes import HashingEmbedder
from packages.evaluation.fixtures import FixtureSet
from src.documents.chunker import chunk_document_pages
from src.documents.retriever import DocumentRetriever
from src.documents.vector_store import ChromaDocumentStore
from src.memory.session_memory import SessionMemory
from src.storage.query_executor import execute_read_query
from src.storage.sqlite_store import SQLiteStore, StoredTable


@dataclass(frozen=True)
class _Page:
    page_number: int
    text: str


class EvaluationEnvironment:
    def __init__(self, fixtures: FixtureSet, workdir: Path) -> None:
        self._fixtures = fixtures
        self._workdir = workdir
        self._tables: dict[str, StoredTable] = {}
        self._expected_rows: dict[str, int] = {}
        self._retrievers: dict[str, DocumentRetriever] = {}

    @property
    def thresholds(self) -> dict[str, Any]:
        return self._fixtures.thresholds

    def stored_table(self, name: str) -> StoredTable:
        if name not in self._tables:
            spec = self._fixtures.datasets[name]
            frame = _frame(spec)
            store = SQLiteStore(self._workdir / "sqlite" / f"{name}.db")
            self._tables[name] = store.save_dataframe(frame)
            self._expected_rows[name] = len(frame)
        return self._tables[name]

    def dataset_intact(self, name: str) -> bool:
        """True when the table still exists with its original row count."""
        table = self.stored_table(name)
        try:
            result = execute_read_query(
                table.database_path,
                f'SELECT COUNT(*) AS "n" FROM "{table.table_name}"',
                allowed_tables={table.table_name},
            )
        except Exception:
            return False
        return int(result.iloc[0]["n"]) == self._expected_rows[name]

    def retriever(self, name: str) -> DocumentRetriever:
        if name not in self._retrievers:
            spec = self._fixtures.corpora[name]
            settings = self._fixtures.retrieval
            pages = [_Page(index, text) for index, text in enumerate(spec["pages"], start=1)]
            chunks = chunk_document_pages(
                pages,
                spec["filename"],
                chunk_size=int(settings["chunk_size"]),
                overlap=int(settings["overlap"]),
                document_id=name,
            )
            store = ChromaDocumentStore(
                persist_dir=self._workdir / "vectors" / name,
                embedder=HashingEmbedder(),
            )
            store.replace_chunks(chunks)
            self._retrievers[name] = DocumentRetriever(
                store,
                max_distance=float(settings["max_distance"]),
                default_top_k=int(settings["top_k"]),
            )
        return self._retrievers[name]

    def dense_retriever(self, name: str) -> DocumentRetriever:
        """Dense-only view of the same index: the negative control for lexical cases."""
        hybrid = self.retriever(name)
        return DocumentRetriever(
            hybrid.store,
            max_distance=hybrid.max_distance,
            default_top_k=hybrid.default_top_k,
            hybrid=False,
        )

    def close(self) -> None:
        for retriever in self._retrievers.values():
            retriever.close()
        self._retrievers.clear()


def build_memory(spec: dict[str, Any]) -> SessionMemory:
    defaults: dict[str, Any] = {
        "row_count": 12,
        "column_count": 6,
        "mapped_fields": {},
        "missing_fields": [],
        "available_capabilities": [],
        "unavailable_capabilities": {},
        "analytics_highlights": [],
        "anomaly_findings": [],
        "chart_summaries": [],
        "report_sections": {},
        "document_summary": [],
        "limitations": [],
    }
    return SessionMemory(**{**defaults, **spec})


def _frame(spec: dict[str, Any]) -> pd.DataFrame:
    if "generate" in spec:
        rows = int(spec["generate"]["rows"])
        columns = spec["generate"]["columns"]
        return pd.DataFrame(
            [[index, (index * 7) % 101] for index in range(1, rows + 1)],
            columns=columns,
        )
    return pd.DataFrame(spec["rows"], columns=spec["columns"])
