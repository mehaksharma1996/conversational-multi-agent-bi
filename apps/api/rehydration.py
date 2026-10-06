"""Rebuild datasets, analyses, and reports from persisted inputs after a restart (ADR 0022).

The metadata store keeps only inputs: the upload payload, the requested sheet, the confirmed schema
mapping, and the analysis parameters. This module re-runs the same deterministic services the API
routes use, so a recovered resource equals the one that existed before the restart.
"""

from __future__ import annotations

from pathlib import Path

from config.settings import Settings
from packages.analytics import TabularApplicationService
from packages.analytics.contracts import (
    AnalyzeTabularCommand,
    LoadTabularCommand,
    ProfileTabularCommand,
)
from packages.retrieval import DocumentApplicationService, IndexDocumentsCommand
from src.analytics.pipeline import AnalysisBundle
from src.documents.pgvector_store import psycopg_connection_factory
from src.documents.retriever import DocumentRetriever
from src.ingestion.tabular_loader import LoadedTable
from src.memory.session_memory import SessionMemory, build_session_memory
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping
from src.storage.sqlite_store import SQLiteStore, StoredTable


class ApiRehydrator:
    """Implements ``apps.api.repository.ResourceRehydrator`` with the application services."""

    def __init__(
        self,
        service: TabularApplicationService,
        settings: Settings,
        documents: DocumentApplicationService | None = None,
    ) -> None:
        self._service = service
        self._settings = settings
        self._documents = documents

    def load_table(
        self, payload: bytes, filename: str, sheet: str | int
    ) -> tuple[LoadedTable, DataProfile]:
        table = self._service.load(
            LoadTabularCommand(
                payload=payload,
                filename=filename,
                max_upload_bytes=self._settings.max_tabular_upload_bytes,
                max_rows=self._settings.max_tabular_rows,
                sheet_name=sheet,
            )
        )
        profiled = self._service.profile(ProfileTabularCommand(dataframe=table.dataframe))
        return table, profiled.profile

    def save_table(
        self, workspace_dir: Path, table: LoadedTable, mapping: SchemaMapping
    ) -> StoredTable:
        return SQLiteStore(
            workspace_dir / "sqlite" / "app.db",
            encryption_key=self._settings.sqlite_encryption_key,
            include_sample_values=not self._settings.gemini_exclude_sample_values,
        ).save_dataframe(table.dataframe, canonical_mapping=mapping.mapped_fields())

    def open_documents(
        self,
        tenant_id: str,
        workspace_id: str,
        workspace_dir: Path,
        index_backend: str,
        chunk_count: int,
    ) -> DocumentRetriever:
        """Reopen the persisted index with the backend it was built with and verify its size."""
        settings = self._settings
        if self._documents is None:
            raise ValueError("Document recovery is not configured.")
        return self._documents.open_existing(
            IndexDocumentsCommand(
                documents=(),
                persist_dir=workspace_dir / "vectorstore",
                embedding_model=settings.embedding_model,
                max_pages=settings.max_pdf_pages,
                max_chunks=settings.max_document_chunks,
                retrieval_top_k=settings.retrieval_top_k,
                retrieval_max_distance=settings.retrieval_max_distance,
                retrieval_hybrid=settings.retrieval_hybrid,
                index_backend=index_backend,
                index_scope=f"{tenant_id}/{workspace_id}",
                pgvector_connect=(
                    psycopg_connection_factory(str(settings.postgres_dsn))
                    if index_backend == "pgvector"
                    else None
                ),
                pgvector_auto_migrate=False,
                cache_tenant_id=tenant_id,
                cache_workspace_id=workspace_id,
            ),
            chunk_count,
        )

    def analyze(
        self,
        table: LoadedTable,
        profile: DataProfile,
        mapping: SchemaMapping,
        anomaly_features: tuple[str, ...],
        anomaly_contamination: float,
    ) -> tuple[AnalysisBundle, SessionMemory]:
        bundle = self._service.analyze(
            AnalyzeTabularCommand(
                dataframe=table.dataframe,
                profile=profile,
                schema_mapping=mapping,
                anomaly_features=anomaly_features,
                anomaly_contamination=anomaly_contamination,
            )
        )
        memory = build_session_memory(
            profile=profile,
            schema_mapping=mapping,
            capability_report=bundle.capability_report,
            analytics_report=bundle.analytics_report,
            anomaly_report=bundle.anomaly_report,
            chart_specs=bundle.chart_specs,
            business_report=bundle.business_report,
            document_status=None,
        )
        return bundle, memory
