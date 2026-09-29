"""Versioned FastAPI routes for the initial tabular vertical slice."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, UploadFile, status

from apps.api.dependencies import (
    get_api_settings,
    get_identity,
    get_repository,
    get_tabular_service,
)
from apps.api.errors import (
    STANDARD_ERROR_RESPONSES,
    ApiError,
    ResourceConflictError,
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

router = APIRouter(prefix="/api/v1")

IdentityDependency = Annotated[IdentityContext, Depends(get_identity)]
RepositoryDependency = Annotated[LocalResourceRepository, Depends(get_repository)]
ServiceDependency = Annotated[TabularApplicationService, Depends(get_tabular_service)]
SettingsDependency = Annotated[Settings, Depends(get_api_settings)]


@router.post(
    "/workspaces",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["workspaces"],
)
def create_workspace(
    identity: IdentityDependency,
    repository: RepositoryDependency,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
) -> WorkspaceResponse:
    record = repository.create_workspace(
        tenant_id=identity.tenant_id,
        authentication_mode=identity.authentication_mode,
        idempotency_key=idempotency_key,
    )
    return workspace_response(record)


@router.get(
    "/workspaces/{workspace_id}",
    response_model=WorkspaceResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["workspaces"],
)
def get_workspace(
    workspace_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
) -> WorkspaceResponse:
    return workspace_response(repository.get_workspace(workspace_id, identity.tenant_id))


@router.post(
    "/workspaces/{workspace_id}/tabular-uploads",
    response_model=TabularUploadResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["tabular uploads"],
)
async def upload_tabular_file(
    workspace_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    settings: SettingsDependency,
    file: Annotated[UploadFile, File(description="CSV or Excel source file")],
) -> TabularUploadResponse:
    repository.get_workspace(workspace_id, identity.tenant_id)
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
    return upload_response(record)


@router.get(
    "/tabular-uploads/{upload_id}/sheets",
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
    response_model=DatasetResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["datasets"],
)
def create_dataset(
    upload_id: str,
    command: DatasetCreateRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    service: ServiceDependency,
    settings: SettingsDependency,
) -> DatasetResponse:
    upload = repository.get_upload(upload_id, identity.tenant_id)
    try:
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
    except (TabularLoadError, TabularWorkflowError, ValueError) as exc:
        raise ApiError(422, "invalid_tabular_dataset", str(exc)) from exc

    dataset = repository.create_dataset(
        upload=upload,
        table=table,
        profile=profiled.profile,
        schema_mapping=profiled.suggested_mapping,
        recommended_anomaly_features=profiled.recommended_anomaly_features,
    )
    return dataset_response(dataset)


@router.get(
    "/datasets/{dataset_id}",
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

    updated = repository.confirm_dataset_mapping(
        dataset_id=dataset.id,
        tenant_id=identity.tenant_id,
        schema_mapping=mapping,
        recommended_anomaly_features=recommended_features,
    )
    return dataset_response(updated)


@router.post(
    "/datasets/{dataset_id}/analyses",
    response_model=AnalysisResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["analyses"],
)
def create_analysis(
    dataset_id: str,
    command: AnalysisCreateRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    service: ServiceDependency,
) -> AnalysisResponse:
    dataset = repository.get_dataset(dataset_id, identity.tenant_id)
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
        bundle = service.analyze(
            AnalyzeTabularCommand(
                dataframe=dataset.table.dataframe,
                profile=dataset.profile,
                schema_mapping=dataset.schema_mapping,
                anomaly_features=anomaly_features,
                anomaly_contamination=command.anomaly_contamination,
            )
        )
    except ValueError as exc:
        raise ApiError(422, "invalid_analysis_configuration", str(exc)) from exc
    analysis = repository.create_analysis(
        dataset=dataset,
        anomaly_contamination=command.anomaly_contamination,
        bundle=bundle,
    )
    return analysis_response(analysis)


@router.get(
    "/analyses/{analysis_id}",
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
