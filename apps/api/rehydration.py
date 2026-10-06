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
from src.analytics.pipeline import AnalysisBundle
from src.ingestion.tabular_loader import LoadedTable
from src.memory.session_memory import SessionMemory, build_session_memory
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping
from src.storage.sqlite_store import SQLiteStore, StoredTable


class ApiRehydrator:
    """Implements ``apps.api.repository.ResourceRehydrator`` with the application services."""

    def __init__(self, service: TabularApplicationService, settings: Settings) -> None:
        self._service = service
        self._settings = settings

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
