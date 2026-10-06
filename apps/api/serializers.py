"""Explicit serializers from pandas/domain objects to stable API contracts."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from apps.api.models import (
    AnalysisResponse,
    AnomalyResponse,
    BusinessReportResponse,
    CapabilityResponse,
    ChartResponse,
    ClassificationMetricsResponse,
    ClassificationResponse,
    ColumnProfileResponse,
    ConversationResponse,
    DataProfileResponse,
    DatasetResponse,
    DocumentCollectionResponse,
    ExportResponse,
    FieldMappingResponse,
    JobResponse,
    MessageListResponse,
    MessageResponse,
    ProvenanceResponse,
    ReportResponse,
    ReportSectionResponse,
    SchemaMappingResponse,
    SourceResponse,
    TabularUploadResponse,
    WorkspaceResponse,
)
from apps.api.repository import (
    AnalysisRecord,
    ConversationRecord,
    DatasetRecord,
    DocumentCollectionRecord,
    ExportRecord,
    MessageRecord,
    ReportRecord,
    TabularUploadRecord,
    WorkspaceRecord,
)
from packages.jobs import JobRecord
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping


def workspace_response(record: WorkspaceRecord, retention_hours: int) -> WorkspaceResponse:
    return WorkspaceResponse(
        id=record.id,
        status="active",
        authentication_mode=record.authentication_mode,
        expires_at=record.expires_at,
        retention_hours=retention_hours,
        gemini_configured=record.gemini_configured,
        local_only_mode=record.local_only_mode,
        consent_required=record.gemini_configured,
        consent_accepted=record.consent_accepted_at is not None,
        data_recipients=list(record.data_recipients),
        created_at=record.created_at,
    )


def job_response(record: JobRecord) -> JobResponse:
    """Serialize only content-free job metadata; never expose the in-memory result."""
    return JobResponse(
        id=record.id,
        workspace_id=record.workspace_id,
        operation=record.operation,
        status=record.status,
        progress_stage=record.progress_stage,
        progress_completed=record.progress_completed,
        progress_total=record.progress_total,
        error_category=record.error_category,
        cancel_requested=record.cancel_requested,
        retryable=record.retryable,
        attempt=record.attempt,
        max_attempts=record.max_attempts,
        request_id=record.request_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        expires_at=record.expires_at,
    )


def upload_response(record: TabularUploadRecord) -> TabularUploadResponse:
    return TabularUploadResponse(
        id=record.id,
        workspace_id=record.workspace_id,
        filename=record.filename,
        content_type=record.content_type,
        size_bytes=len(record.payload),
        status="received",
        created_at=record.created_at,
    )


def dataset_response(record: DatasetRecord) -> DatasetResponse:
    return DatasetResponse(
        id=record.id,
        workspace_id=record.workspace_id,
        upload_id=record.upload_id,
        status="ready" if record.mapping_confirmed else "review_required",
        filename=record.table.filename,
        sheet_name=record.table.sheet_name,
        row_count=record.table.row_count,
        column_count=record.table.column_count,
        column_name_mapping=record.table.column_name_mapping,
        column_warnings=record.table.column_warnings,
        profile=_profile_response(record.profile),
        schema_mapping=_mapping_response(
            record.schema_mapping,
            confirmed=record.mapping_confirmed,
            version=record.mapping_version,
        ),
        recommended_anomaly_features=list(record.recommended_anomaly_features),
        created_at=record.created_at,
    )


def analysis_response(record: AnalysisRecord) -> AnalysisResponse:
    bundle = record.bundle
    analytics = bundle.analytics_report
    anomaly = bundle.anomaly_report
    classification = bundle.classification_report
    return AnalysisResponse(
        id=record.id,
        workspace_id=record.workspace_id,
        dataset_id=record.dataset_id,
        dataset_mapping_version=record.dataset_mapping_version,
        status="ready",
        anomaly_contamination=record.anomaly_contamination,
        created_at=record.created_at,
        capabilities=[
            CapabilityResponse(
                name=capability.name,
                available=capability.available,
                reason=capability.reason,
                required_fields=capability.required_fields,
                missing_fields=capability.missing_fields,
            )
            for capability in bundle.capability_report.capabilities
        ],
        dataset_summary=analytics.dataset_summary,
        numeric_summary=_records(analytics.numeric_summary),
        categorical_breakdowns={
            column: _records(frame) for column, frame in analytics.categorical_breakdowns.items()
        },
        amount_by_category={
            column: _records(frame) for column, frame in analytics.amount_by_category.items()
        },
        trend=_records(analytics.trend) if analytics.trend is not None else None,
        analytics_limitations=analytics.limitations,
        anomaly=AnomalyResponse(
            enabled=anomaly.enabled,
            method=anomaly.method,
            feature_columns=anomaly.feature_columns,
            flagged_count=anomaly.flagged_count,
            model_flagged_count=anomaly.model_flagged_count,
            rule_flagged_count=anomaly.rule_flagged_count,
            score_percentiles=anomaly.score_percentiles,
            flagged_rows=_records(anomaly.flagged_rows),
            limitations=anomaly.limitations,
        ),
        classification=ClassificationResponse(
            enabled=classification.enabled,
            reason=classification.reason,
            method=classification.method,
            label_column=classification.label_column,
            positive_label=classification.positive_label,
            feature_columns=classification.feature_columns,
            excluded_columns=classification.excluded_columns,
            metrics=(
                ClassificationMetricsResponse(
                    precision=classification.metrics.precision,
                    recall=classification.metrics.recall,
                    f1=classification.metrics.f1,
                    roc_auc=classification.metrics.roc_auc,
                    pr_auc=classification.metrics.pr_auc,
                    cv_pr_auc=classification.metrics.cv_pr_auc,
                    threshold=classification.metrics.threshold,
                    positive_rate=classification.metrics.positive_rate,
                    true_negative=classification.metrics.true_negative,
                    false_positive=classification.metrics.false_positive,
                    false_negative=classification.metrics.false_negative,
                    true_positive=classification.metrics.true_positive,
                )
                if classification.metrics is not None
                else None
            ),
            review_candidates=_records(classification.review_candidates),
            limitations=classification.limitations,
        ),
        charts=[
            ChartResponse(
                title=chart.title,
                chart_type=chart.chart_type,
                description=chart.description,
                metadata=chart.metadata,
                figure=json.loads(chart.figure.to_json()),
            )
            for chart in bundle.chart_specs
        ],
        report=BusinessReportResponse(
            title=bundle.business_report.title,
            sections=[
                ReportSectionResponse(
                    title=section.title,
                    bullets=section.bullets,
                    body=section.body,
                )
                for section in bundle.business_report.sections
            ],
        ),
    )


def document_collection_response(
    record: DocumentCollectionRecord,
) -> DocumentCollectionResponse:
    return DocumentCollectionResponse(
        id=record.id,
        workspace_id=record.workspace_id,
        status="ready",
        document_count=len(record.filenames),
        page_count=record.page_count,
        chunk_count=record.chunk_count,
        filenames=list(record.filenames),
        created_at=record.created_at,
    )


def conversation_response(record: ConversationRecord) -> ConversationResponse:
    return ConversationResponse(
        id=record.id,
        workspace_id=record.workspace_id,
        status="active",
        dataset_id=record.dataset_id,
        document_collection_id=record.document_collection_id,
        message_count=len(record.message_ids),
        created_at=record.created_at,
    )


def message_response(record: MessageRecord) -> MessageResponse:
    return MessageResponse(
        id=record.id,
        conversation_id=record.conversation_id,
        role="assistant",
        status=record.status,
        question=record.question,
        answer=record.answer,
        route=record.route,
        sql=record.sql,
        rows=_records(record.dataframe) if record.dataframe is not None else None,
        sources=[SourceResponse(citation=source) for source in record.sources],
        provenance=ProvenanceResponse(
            grounding_status=record.diagnostics.grounding_status,
            source_count=record.diagnostics.source_count,
            citation_count=record.diagnostics.citation_count,
            invalid_citation_count=record.diagnostics.invalid_citation_count,
            unverified_quote_count=record.diagnostics.unverified_quote_count,
            criteria_provenance=record.diagnostics.criteria_provenance,
            hybrid_fell_back_to_documents=record.diagnostics.hybrid_fell_back_to_documents,
        ),
        request_id=record.request_id,
        created_at=record.created_at,
    )


def message_list_response(
    conversation_id: str,
    records: list[MessageRecord],
    next_cursor: str | None,
) -> MessageListResponse:
    return MessageListResponse(
        conversation_id=conversation_id,
        messages=[message_response(record) for record in records],
        next_cursor=next_cursor,
    )


def report_response(record: ReportRecord) -> ReportResponse:
    return ReportResponse(
        id=record.id,
        analysis_id=record.analysis_id,
        workspace_id=record.workspace_id,
        status="ready",
        formats=["markdown", "pdf"],
        created_at=record.created_at,
    )


def export_response(record: ExportRecord) -> ExportResponse:
    return ExportResponse(
        id=record.id,
        message_id=record.message_id,
        workspace_id=record.workspace_id,
        status="ready",
        format=record.format,
        filename=record.filename,
        media_type=record.media_type,
        size_bytes=len(record.payload),
        created_at=record.created_at,
    )


def _profile_response(profile: DataProfile) -> DataProfileResponse:
    return DataProfileResponse(
        row_count=profile.row_count,
        column_count=profile.column_count,
        duplicate_row_count=profile.duplicate_row_count,
        columns=[
            ColumnProfileResponse(
                name=column.name,
                dtype=column.dtype,
                non_null_count=column.non_null_count,
                missing_count=column.missing_count,
                missing_ratio=column.missing_ratio,
                unique_count=column.unique_count,
                unique_ratio=column.unique_ratio,
                inferred_type=column.inferred_type,
                sample_values=column.sample_values,
            )
            for column in profile.columns
        ],
        numeric_columns=profile.numeric_columns,
        date_columns=profile.date_columns,
        categorical_columns=profile.categorical_columns,
        boolean_columns=profile.boolean_columns,
        possible_id_columns=profile.possible_id_columns,
        possible_label_columns=profile.possible_label_columns,
    )


def _mapping_response(
    mapping: SchemaMapping,
    *,
    confirmed: bool,
    version: int,
) -> SchemaMappingResponse:
    return SchemaMappingResponse(
        status="confirmed" if confirmed else "draft",
        version=version,
        mappings={
            field: FieldMappingResponse(
                canonical_field=value.canonical_field,
                source_column=value.source_column,
                confidence=value.confidence,
                reason=value.reason,
            )
            for field, value in mapping.mappings.items()
        },
    )


def _records(dataframe: pd.DataFrame) -> list[dict[str, Any]]:
    if dataframe.empty:
        return []
    payload = dataframe.to_json(orient="records", date_format="iso")
    records: list[dict[str, Any]] = json.loads(payload)
    return records
