"""Pydantic request and response contracts for API v1."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.jobs import JobStatus


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


class JobResponse(StrictModel):
    id: str
    workspace_id: str
    operation: str
    status: JobStatus
    progress_stage: str | None
    progress_completed: int
    progress_total: int | None
    error_category: str | None
    cancel_requested: bool
    retryable: bool
    attempt: int
    max_attempts: int
    request_id: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class JobListResponse(StrictModel):
    items: list[JobResponse]
    next_cursor: str | None


class WorkspaceResponse(StrictModel):
    id: str
    status: Literal["active"]
    authentication_mode: str
    expires_at: datetime
    gemini_configured: bool
    local_only_mode: bool
    consent_required: bool
    consent_accepted: bool
    data_recipients: list[str] = Field(default_factory=list)
    created_at: datetime


class ConsentUpdateRequest(StrictModel):
    accepted: Literal[True]
    notice_version: str = Field(default="2026-09", min_length=1, max_length=32)


class ConsentResponse(StrictModel):
    workspace_id: str
    required: bool
    accepted: bool
    notice_version: str | None
    accepted_at: datetime | None
    data_recipients: list[str] = Field(default_factory=list)


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


class ClassificationMetricsResponse(StrictModel):
    precision: float
    recall: float
    f1: float
    roc_auc: float
    pr_auc: float
    cv_pr_auc: float
    threshold: float
    positive_rate: float
    true_negative: int
    false_positive: int
    false_negative: int
    true_positive: int


class ClassificationResponse(StrictModel):
    enabled: bool
    reason: str
    method: str
    label_column: str | None
    positive_label: str | None
    feature_columns: list[str]
    excluded_columns: dict[str, str]
    metrics: ClassificationMetricsResponse | None
    review_candidates: list[dict[str, Any]]
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
    classification: ClassificationResponse
    charts: list[ChartResponse]
    report: BusinessReportResponse


class DocumentCollectionResponse(StrictModel):
    id: str
    workspace_id: str
    status: Literal["ready"]
    document_count: int
    page_count: int
    chunk_count: int
    filenames: list[str]
    created_at: datetime


class ConversationCreateRequest(StrictModel):
    dataset_id: str | None = None
    document_collection_id: str | None = None


class ConversationResponse(StrictModel):
    id: str
    workspace_id: str
    status: Literal["active"]
    dataset_id: str | None
    document_collection_id: str | None
    message_count: int
    created_at: datetime


class MessageCreateRequest(StrictModel):
    question: str = Field(min_length=1, max_length=4_000)
    require_sql_approval: bool = False


class MessageApprovalRequest(StrictModel):
    decision: Literal["approve", "reject"]
    sql: str | None = Field(default=None, min_length=1, max_length=20_000)


class SourceResponse(StrictModel):
    citation: str


class ProvenanceResponse(StrictModel):
    """Content-free description of how an answer was checked.

    These are structural checks (citation numbers, verbatim quotes, literal
    criteria overlap), not proof that the answer is correct.
    """

    grounding_status: Literal["not_applicable", "checked_no_issues", "uncited", "warnings"]
    source_count: int
    citation_count: int
    invalid_citation_count: int
    unverified_quote_count: int
    criteria_provenance: Literal[
        "not_applicable",
        "traced",
        "unreferenced",
        "no_structured_criteria",
        "excerpt_fallback",
    ]
    hybrid_fell_back_to_documents: bool


class MessageResponse(StrictModel):
    id: str
    conversation_id: str
    role: Literal["assistant"]
    status: Literal["complete", "pending_approval", "rejected"]
    question: str
    answer: str
    route: Literal["memory", "sql", "rag", "hybrid", "unsupported"]
    sql: str | None
    rows: list[dict[str, Any]] | None
    sources: list[SourceResponse]
    provenance: ProvenanceResponse
    request_id: str
    created_at: datetime


class MessageListResponse(StrictModel):
    conversation_id: str
    messages: list[MessageResponse]


class ReportCreateRequest(StrictModel):
    include_charts: bool = True


class ReportResponse(StrictModel):
    id: str
    analysis_id: str
    workspace_id: str
    status: Literal["ready"]
    formats: list[Literal["markdown", "pdf"]]
    created_at: datetime


class ExportCreateRequest(StrictModel):
    format: Literal["csv", "xlsx"]


class ExportResponse(StrictModel):
    id: str
    message_id: str
    workspace_id: str
    status: Literal["ready"]
    format: Literal["csv", "xlsx"]
    filename: str
    media_type: str
    size_bytes: int
    created_at: datetime
