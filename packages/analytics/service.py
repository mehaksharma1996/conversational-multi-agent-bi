"""Application service coordinating the existing deterministic analytics core."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from packages.analytics.contracts import (
    AnalyzeTabularCommand,
    ConfirmSchemaCommand,
    ListWorkbookSheetsCommand,
    LoadTabularCommand,
    ProfiledTabularData,
    ProfileTabularCommand,
)
from src.analytics.anomaly_detection import recommend_anomaly_features
from src.analytics.pipeline import AnalysisBundle, build_analysis_bundle
from src.ingestion.tabular_loader import LoadedTable, list_excel_sheets, load_tabular_file
from src.profiling.data_profiler import DataProfile, profile_dataframe
from src.profiling.schema_mapper import SchemaMapping, map_schema


class TabularWorkflowError(ValueError):
    """Base error for a rejected tabular application command."""


class TabularUploadLimitError(TabularWorkflowError):
    """Raised before parsing when an upload exceeds the byte limit."""


class TabularRowLimitError(TabularWorkflowError):
    """Raised when bounded parsing proves an upload exceeds the row limit."""


class TabularApplicationService:
    """Coordinate tabular use cases without depending on a UI or HTTP framework."""

    def list_workbook_sheets(self, command: ListWorkbookSheetsCommand) -> tuple[str, ...]:
        extension = Path(command.filename).suffix.lower()
        if extension not in {".xls", ".xlsx"}:
            return ()
        return tuple(list_excel_sheets(BytesIO(command.payload)))

    def load(self, command: LoadTabularCommand) -> LoadedTable:
        if command.max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be at least 1.")
        if command.max_rows < 1:
            raise ValueError("max_rows must be at least 1.")
        if len(command.payload) > command.max_upload_bytes:
            limit_mb = command.max_upload_bytes / (1024 * 1024)
            raise TabularUploadLimitError(f"Tabular uploads are limited to {limit_mb:g} MB.")

        table = load_tabular_file(
            file=BytesIO(command.payload),
            filename=command.filename,
            max_rows=command.max_rows + 1,
            sheet_name=command.sheet_name,
        )
        if table.row_count > command.max_rows:
            raise TabularRowLimitError(
                f"The upload contains {table.row_count:,} rows; "
                f"the current limit is {command.max_rows:,}."
            )
        return table

    def profile(self, command: ProfileTabularCommand) -> ProfiledTabularData:
        profile = profile_dataframe(command.dataframe)
        suggested_mapping = map_schema(profile)
        return ProfiledTabularData(
            profile=profile,
            suggested_mapping=suggested_mapping,
            recommended_anomaly_features=tuple(
                recommend_anomaly_features(profile, suggested_mapping)
            ),
        )

    def confirm_schema(self, command: ConfirmSchemaCommand) -> SchemaMapping:
        return map_schema(command.profile, overrides=command.overrides)

    def recommend_anomaly_features(
        self,
        profile: DataProfile,
        schema_mapping: SchemaMapping,
    ) -> tuple[str, ...]:
        return tuple(recommend_anomaly_features(profile, schema_mapping))

    def analyze(self, command: AnalyzeTabularCommand) -> AnalysisBundle:
        return build_analysis_bundle(
            dataframe=command.dataframe,
            profile=command.profile,
            schema_mapping=command.schema_mapping,
            anomaly_features=list(command.anomaly_features),
            anomaly_contamination=command.anomaly_contamination,
            document_status=command.document_status,
        )
