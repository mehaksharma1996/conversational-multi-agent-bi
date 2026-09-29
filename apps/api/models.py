"""Pydantic request and response contracts for API v1."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(StrictModel):
    field: str | None = None
    message: str
    type: str | None = None


class ErrorBody(StrictModel):
    code: str
    message: str
    request_id: str
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(StrictModel):
    error: ErrorBody


class HealthResponse(StrictModel):
    status: Literal["alive", "ready"]


class WorkspaceResponse(StrictModel):
    id: str
    status: Literal["active"]
    authentication_mode: str
    created_at: datetime


class TabularUploadResponse(StrictModel):
    id: str
    workspace_id: str
    filename: str
    content_type: str | None
    size_bytes: int
    status: Literal["received"]
    created_at: datetime


class WorkbookSheetsResponse(StrictModel):
    upload_id: str
    sheets: list[str]


class DatasetCreateRequest(StrictModel):
    sheet_name: str | int = 0


class ColumnProfileResponse(StrictModel):
    name: str
    dtype: str
    non_null_count: int
    missing_count: int
    missing_ratio: float
    unique_count: int
    unique_ratio: float
    inferred_type: str
    sample_values: list[str]


class DataProfileResponse(StrictModel):
    row_count: int
    column_count: int
    duplicate_row_count: int
    columns: list[ColumnProfileResponse]
    numeric_columns: list[str]
    date_columns: list[str]
    categorical_columns: list[str]
    boolean_columns: list[str]
    possible_id_columns: list[str]
    possible_label_columns: list[str]


class FieldMappingResponse(StrictModel):
    canonical_field: str
    source_column: str | None
    confidence: float
    reason: str


class SchemaMappingResponse(StrictModel):
    status: Literal["draft", "confirmed"]
    version: int
    mappings: dict[str, FieldMappingResponse]


class DatasetResponse(StrictModel):
    id: str
    workspace_id: str
    upload_id: str
    status: Literal["review_required", "ready"]
    filename: str
    sheet_name: str | None
    row_count: int
    column_count: int
    column_name_mapping: dict[str, str]
    column_warnings: dict[str, list[str]]
    profile: DataProfileResponse
    schema_mapping: SchemaMappingResponse
    recommended_anomaly_features: list[str]
    created_at: datetime


class SchemaMappingUpdateRequest(StrictModel):
    amount: str | None = None
    date: str | None = None
    customer_id: str | None = None
    merchant: str | None = None
    location: str | None = None
    label: str | None = None


class AnalysisCreateRequest(StrictModel):
    anomaly_features: list[str] | None = None
    anomaly_contamination: float = Field(default=0.05, gt=0, le=0.5)


class CapabilityResponse(StrictModel):
    name: str
    available: bool
    reason: str
    required_fields: list[str]
    missing_fields: list[str]


class AnomalyResponse(StrictModel):
    enabled: bool
    method: str
    feature_columns: list[str]
    flagged_count: int
    model_flagged_count: int
    rule_flagged_count: int
    score_percentiles: dict[str, float]
    flagged_rows: list[dict[str, Any]]
    limitations: list[str]


class ChartResponse(StrictModel):
    title: str
    chart_type: str
    description: str
    metadata: dict[str, str | list[str]]
    figure: dict[str, Any]


class ReportSectionResponse(StrictModel):
    title: str
    bullets: list[str]
    body: str | None


class BusinessReportResponse(StrictModel):
    title: str
    sections: list[ReportSectionResponse]


class AnalysisResponse(StrictModel):
    id: str
    workspace_id: str
    dataset_id: str
    dataset_mapping_version: int
    status: Literal["ready"]
    anomaly_contamination: float
    created_at: datetime
    capabilities: list[CapabilityResponse]
    dataset_summary: dict[str, int]
    numeric_summary: list[dict[str, Any]]
    categorical_breakdowns: dict[str, list[dict[str, Any]]]
    amount_by_category: dict[str, list[dict[str, Any]]]
    trend: list[dict[str, Any]] | None
    analytics_limitations: list[str]
    anomaly: AnomalyResponse
    charts: list[ChartResponse]
    report: BusinessReportResponse
