"""Framework-neutral commands and results for the tabular workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.analytics.anomaly_detection import DEFAULT_CONTAMINATION
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping


@dataclass(frozen=True)
class ListWorkbookSheetsCommand:
    """Request sheet discovery for an uploaded workbook payload."""

    payload: bytes
    filename: str


@dataclass(frozen=True)
class LoadTabularCommand:
    """Request bounded parsing and normalization of a tabular upload."""

    payload: bytes
    filename: str
    max_upload_bytes: int
    max_rows: int
    sheet_name: str | int = 0


@dataclass(frozen=True)
class ProfileTabularCommand:
    """Request deterministic profiling and schema suggestions."""

    dataframe: pd.DataFrame


@dataclass(frozen=True)
class ProfiledTabularData:
    """Profile plus the initial, reviewable analysis defaults."""

    profile: DataProfile
    suggested_mapping: SchemaMapping
    recommended_anomaly_features: tuple[str, ...]


@dataclass(frozen=True)
class ConfirmSchemaCommand:
    """Request validation of a user-reviewed canonical schema mapping."""

    profile: DataProfile
    overrides: dict[str, str | None]


@dataclass(frozen=True)
class AnalyzeTabularCommand:
    """Request deterministic analysis for a confirmed schema."""

    dataframe: pd.DataFrame
    profile: DataProfile
    schema_mapping: SchemaMapping
    anomaly_features: tuple[str, ...]
    anomaly_contamination: float = DEFAULT_CONTAMINATION
    document_status: dict[str, Any] | None = field(default=None)
