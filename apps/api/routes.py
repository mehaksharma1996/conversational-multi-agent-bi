"""Versioned FastAPI routes for the initial tabular vertical slice."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, UploadFile, status

from apps.api.authorization import Capability, requires
from apps.api.dependencies import (
    get_api_settings,
    get_identity,
    get_observability,
    get_rate_limiter,
    get_repository,
    get_tabular_service,
)
from apps.api.errors import (
    STANDARD_ERROR_RESPONSES,
    ApiError,
    ResourceConflictError,
)
from apps.api.http_semantics import (
    NON_IDEMPOTENT_CREATE_SEMANTICS,
    WORKSPACE_CREATE_SEMANTICS,
)
from apps.api.models import (
    AnalysisCreateRequest,
    AnalysisResponse,
    DatasetCreateRequest,
    DatasetResponse,
    SchemaMappingUpdateRequest,
    TabularUploadResponse,
    WorkbookSheetsResponse,
    WorkspaceResponse,
)
from apps.api.observability import ApiObservability
from apps.api.rate_limit import InMemoryRateLimiter, enforce_rate_limit
from apps.api.repository import LocalResourceRepository
from apps.api.serializers import (
    analysis_response,
    dataset_response,
    upload_response,
    workspace_response,
)
from config.settings import Settings
from packages.analytics import (
    AnalyzeTabularCommand,
    ConfirmSchemaCommand,
    ListWorkbookSheetsCommand,
    LoadTabularCommand,
    ProfileTabularCommand,
    TabularApplicationService,
    TabularWorkflowError,
)
from packages.connectors import IdentityContext
from src.ingestion.tabular_loader import SUPPORTED_TABULAR_EXTENSIONS, TabularLoadError
from src.memory.session_memory import build_session_memory
from src.storage.sqlite_store import SQLiteStore

router = APIRouter(prefix="/api/v1")

IdentityDependency = Annotated[IdentityContext, Depends(get_identity)]
RepositoryDependency = Annotated[LocalResourceRepository, Depends(get_repository)]
ServiceDependency = Annotated[TabularApplicationService, Depends(get_tabular_service)]
SettingsDependency = Annotated[Settings, Depends(get_api_settings)]
ObservabilityDependency = Annotated[ApiObservability, Depends(get_observability)]
RateLimiterDependency = Annotated[InMemoryRateLimiter, Depends(get_rate_limiter)]


@router.post(
    "/workspaces",
    dependencies=[requires(Capability.DATA_WRITE)],
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["workspaces"],
    description=WORKSPACE_CREATE_SEMANTICS,
)
def create_workspace(
    identity: IdentityDependency,
    repository: RepositoryDependency,
    settings: SettingsDependency,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
) -> WorkspaceResponse:
    record = repository.create_workspace(
        tenant_id=identity.tenant_id,
        authentication_mode=identity.authentication_mode,
        idempotency_key=idempotency_key,
        gemini_configured=settings.hosted_model_configured,
        data_recipients=settings.hosted_recipients(),
        local_only_mode=settings.local_only_mode,
    )
    return workspace_response(record, repository.retention_hours)


@router.get(
    "/workspaces/{workspace_id}",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=WorkspaceResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["workspaces"],
)
def get_workspace(
    workspace_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
) -> WorkspaceResponse:
    return workspace_response(
        repository.get_workspace(workspace_id, identity.tenant_id), repository.retention_hours
    )


@router.post(
    "/workspaces/{workspace_id}/tabular-uploads",
    dependencies=[requires(Capability.DATA_WRITE)],
    response_model=TabularUploadResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["tabular uploads"],
    description=NON_IDEMPOTENT_CREATE_SEMANTICS,
)
async def upload_tabular_file(
    workspace_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    settings: SettingsDependency,
    observability: ObservabilityDependency,
    rate_limiter: RateLimiterDependency,
    file: Annotated[UploadFile, File(description="CSV or Excel source file")],
) -> TabularUploadResponse:
    repository.get_workspace(workspace_id, identity.tenant_id)
    enforce_rate_limit(
        rate_limiter,
        operation="tabular_upload",
        tenant_id=identity.tenant_id,
        workspace_id=workspace_id,
        limit=settings.rate_limit_tabular_uploads,
        window_seconds=settings.rate_limit_window_seconds,
    )
    filename = _safe_filename(file.filename)
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_TABULAR_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_TABULAR_EXTENSIONS))
        raise ApiError(
            422,
            "unsupported_tabular_type",
            f"Supported tabular file types are: {supported}.",
        )

    try:
        payload = await file.read(settings.max_tabular_upload_bytes + 1)
    finally:
        await file.close()
    if len(payload) > settings.max_tabular_upload_bytes:
        limit_mb = settings.max_tabular_upload_bytes / (1024 * 1024)
        raise ApiError(
            413,
            "tabular_upload_too_large",
            f"Tabular uploads are limited to {limit_mb:g} MB.",
        )

    record = repository.create_upload(
        workspace_id=workspace_id,
        tenant_id=identity.tenant_id,
        filename=filename,
        content_type=file.content_type,
        payload=payload,
    )
    file_format = extension.lstrip(".")
    observability.telemetry.emit(
        "upload.tabular",
        tenant_id=identity.tenant_id,
        size_bytes=len(payload),
        format=file_format,
    )
    observability.audit(
        "tabular.uploaded",
        identity,
        resource_id=record.id,
        size_bytes=len(payload),
        format=file_format,
    )
    return upload_response(record)


@router.get(
    "/tabular-uploads/{upload_id}/sheets",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=WorkbookSheetsResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["tabular uploads"],
)
def list_workbook_sheets(
    upload_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    service: ServiceDependency,
) -> WorkbookSheetsResponse:
    upload = repository.get_upload(upload_id, identity.tenant_id)
    try:
        sheets = service.list_workbook_sheets(
            ListWorkbookSheetsCommand(payload=upload.payload, filename=upload.filename)
        )
    except TabularLoadError as exc:
        raise ApiError(422, "invalid_tabular_upload", str(exc)) from exc
    return WorkbookSheetsResponse(upload_id=upload.id, sheets=list(sheets))


@router.post(
    "/tabular-uploads/{upload_id}/dataset",
    dependencies=[requires(Capability.DATA_WRITE)],
    response_model=DatasetResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["datasets"],
    description=NON_IDEMPOTENT_CREATE_SEMANTICS,
)
def create_dataset(
    upload_id: str,
    command: DatasetCreateRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    service: ServiceDependency,
    settings: SettingsDependency,
    observability: ObservabilityDependency,
) -> DatasetResponse:
    upload = repository.get_upload(upload_id, identity.tenant_id)
    try:
        with observability.operation("dataset.create", identity) as operation:
            table = service.load(
                LoadTabularCommand(
                    payload=upload.payload,
                    filename=upload.filename,
                    max_upload_bytes=settings.max_tabular_upload_bytes,
                    max_rows=settings.max_tabular_rows,
                    sheet_name=command.sheet_name,
                )
            )
            profiled = service.profile(ProfileTabularCommand(dataframe=table.dataframe))
            operation.set(row_count=table.row_count, column_count=table.column_count)
    except (TabularLoadError, TabularWorkflowError, ValueError) as exc:
        raise ApiError(422, "invalid_tabular_dataset", str(exc)) from exc

    dataset = repository.create_dataset(
        upload=upload,
        table=table,
        profile=profiled.profile,
        schema_mapping=profiled.suggested_mapping,
        recommended_anomaly_features=profiled.recommended_anomaly_features,
        requested_sheet=command.sheet_name,
    )
    observability.audit(
        "dataset.created",
        identity,
        resource_id=dataset.id,
        row_count=table.row_count,
        column_count=table.column_count,
    )
    return dataset_response(dataset)


@router.get(
    "/datasets/{dataset_id}",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=DatasetResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["datasets"],
)
def get_dataset(
    dataset_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
) -> DatasetResponse:
    return dataset_response(repository.get_dataset(dataset_id, identity.tenant_id))


@router.put(
    "/datasets/{dataset_id}/schema-mapping",
    dependencies=[requires(Capability.DATA_WRITE)],
    response_model=DatasetResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["datasets"],
)
def confirm_schema_mapping(
    dataset_id: str,
    command: SchemaMappingUpdateRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    service: ServiceDependency,
    settings: SettingsDependency,
    observability: ObservabilityDependency,
) -> DatasetResponse:
    dataset = repository.get_dataset(dataset_id, identity.tenant_id)
    try:
        mapping = service.confirm_schema(
            ConfirmSchemaCommand(
                profile=dataset.profile,
                overrides=command.model_dump(),
            )
        )
        recommended_features = service.recommend_anomaly_features(dataset.profile, mapping)
    except ValueError as exc:
        raise ApiError(422, "invalid_schema_mapping", str(exc)) from exc

    with observability.operation("dataset.confirm_schema", identity) as operation:
        stored_table = SQLiteStore(
            repository.workspace_dir(dataset.workspace_id, identity.tenant_id)
            / "sqlite"
            / "app.db",
            encryption_key=settings.sqlite_encryption_key,
            include_sample_values=not settings.gemini_exclude_sample_values,
        ).save_dataframe(
            dataset.table.dataframe,
            canonical_mapping=mapping.mapped_fields(),
        )
        operation.set(row_count=stored_table.row_count, column_count=stored_table.column_count)
    updated = repository.confirm_dataset_mapping(
        dataset_id=dataset.id,
        tenant_id=identity.tenant_id,
        schema_mapping=mapping,
        recommended_anomaly_features=recommended_features,
        stored_table=stored_table,
    )
    observability.audit(
        "dataset.schema_confirmed",
        identity,
        resource_id=updated.id,
        mapping_version=updated.mapping_version,
    )
    return dataset_response(updated)


@router.post(
    "/datasets/{dataset_id}/analyses",
    dependencies=[requires(Capability.ANALYSIS_RUN)],
    response_model=AnalysisResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["analyses"],
    description=NON_IDEMPOTENT_CREATE_SEMANTICS,
)
def create_analysis(
    dataset_id: str,
    command: AnalysisCreateRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    service: ServiceDependency,
    settings: SettingsDependency,
    observability: ObservabilityDependency,
    rate_limiter: RateLimiterDependency,
) -> AnalysisResponse:
    dataset = repository.get_dataset(dataset_id, identity.tenant_id)
    enforce_rate_limit(
        rate_limiter,
        operation="analysis",
        tenant_id=identity.tenant_id,
        workspace_id=dataset.workspace_id,
        limit=settings.rate_limit_analyses,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if not dataset.mapping_confirmed:
        raise ResourceConflictError(
            "schema_mapping_not_confirmed",
            "Confirm the dataset schema mapping before running analysis.",
        )
    anomaly_features = (
        tuple(command.anomaly_features)
        if command.anomaly_features is not None
        else dataset.recommended_anomaly_features
    )
    try:
        with observability.operation("analysis.run", identity) as operation:
            bundle = service.analyze(
                AnalyzeTabularCommand(
                    dataframe=dataset.table.dataframe,
                    profile=dataset.profile,
                    schema_mapping=dataset.schema_mapping,
                    anomaly_features=anomaly_features,
                    anomaly_contamination=command.anomaly_contamination,
                )
            )
            operation.set(
                row_count=dataset.table.row_count,
                mapping_version=dataset.mapping_version,
                anomaly_flagged_count=bundle.anomaly_report.flagged_count,
            )
    except ValueError as exc:
        raise ApiError(422, "invalid_analysis_configuration", str(exc)) from exc
    documents = repository.latest_document_collection_for_workspace(
        dataset.workspace_id,
        identity.tenant_id,
    )
    document_status = (
        {
            "document_count": len(documents.filenames),
            "chunk_count": documents.chunk_count,
            "filenames": list(documents.filenames),
        }
        if documents is not None
        else None
    )
    memory = build_session_memory(
        profile=dataset.profile,
        schema_mapping=dataset.schema_mapping,
        capability_report=bundle.capability_report,
        analytics_report=bundle.analytics_report,
        anomaly_report=bundle.anomaly_report,
        chart_specs=bundle.chart_specs,
        business_report=bundle.business_report,
        document_status=document_status,
    )
    analysis = repository.create_analysis(
        dataset=dataset,
        anomaly_contamination=command.anomaly_contamination,
        bundle=bundle,
        memory=memory,
        anomaly_features=anomaly_features,
    )
    observability.audit(
        "analysis.executed",
        identity,
        resource_id=analysis.id,
        mapping_version=analysis.dataset_mapping_version,
        anomaly_features_count=len(anomaly_features),
    )
    return analysis_response(analysis)


@router.get(
    "/analyses/{analysis_id}",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=AnalysisResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["analyses"],
)
def get_analysis(
    analysis_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
) -> AnalysisResponse:
    return analysis_response(repository.get_analysis(analysis_id, identity.tenant_id))


def _safe_filename(filename: str | None) -> str:
    if filename is None or not filename.strip():
        raise ApiError(422, "missing_filename", "The upload must include a filename.")
    safe_name = Path(filename.replace("\\", "/")).name
    if not safe_name:
        raise ApiError(422, "missing_filename", "The upload must include a filename.")
    return safe_name
