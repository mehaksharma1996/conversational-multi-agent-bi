"""Lock-protected process-local resources for the initial API vertical slice."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from apps.api.errors import ResourceNotFoundError
from src.analytics.pipeline import AnalysisBundle
from src.ingestion.tabular_loader import LoadedTable
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping


def _resource_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WorkspaceRecord:
    id: str
    tenant_id: str
    authentication_mode: str
    created_at: datetime


@dataclass(frozen=True)
class TabularUploadRecord:
    id: str
    workspace_id: str
    tenant_id: str
    filename: str
    content_type: str | None
    payload: bytes
    created_at: datetime


@dataclass(frozen=True)
class DatasetRecord:
    id: str
    workspace_id: str
    upload_id: str
    tenant_id: str
    table: LoadedTable
    profile: DataProfile
    schema_mapping: SchemaMapping
    recommended_anomaly_features: tuple[str, ...]
    mapping_confirmed: bool
    mapping_version: int
    created_at: datetime


@dataclass(frozen=True)
class AnalysisRecord:
    id: str
    workspace_id: str
    dataset_id: str
    tenant_id: str
    dataset_mapping_version: int
    anomaly_contamination: float
    bundle: AnalysisBundle
    created_at: datetime


class LocalResourceRepository:
    """Initial single-process repository with ownership checks on every lookup.

    The API contract is intentionally independent of this implementation. A
    durable repository can replace it in a later persistence phase without
    changing resource identifiers or response schemas.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._workspaces: dict[str, WorkspaceRecord] = {}
        self._workspace_creation_keys: dict[tuple[str, str], str] = {}
        self._uploads: dict[str, TabularUploadRecord] = {}
        self._datasets: dict[str, DatasetRecord] = {}
        self._analyses: dict[str, AnalysisRecord] = {}

    def ready(self) -> bool:
        return True

    def create_workspace(
        self,
        tenant_id: str,
        authentication_mode: str,
        idempotency_key: str | None,
    ) -> WorkspaceRecord:
        with self._lock:
            if idempotency_key is not None:
                existing_id = self._workspace_creation_keys.get((tenant_id, idempotency_key))
                if existing_id is not None:
                    return self._workspaces[existing_id]
            workspace = WorkspaceRecord(
                id=_resource_id("ws"),
                tenant_id=tenant_id,
                authentication_mode=authentication_mode,
                created_at=_now(),
            )
            self._workspaces[workspace.id] = workspace
            if idempotency_key is not None:
                self._workspace_creation_keys[(tenant_id, idempotency_key)] = workspace.id
            return workspace

    def get_workspace(self, workspace_id: str, tenant_id: str) -> WorkspaceRecord:
        with self._lock:
            return self._owned(self._workspaces, workspace_id, tenant_id, "Workspace")

    def create_upload(
        self,
        workspace_id: str,
        tenant_id: str,
        filename: str,
        content_type: str | None,
        payload: bytes,
    ) -> TabularUploadRecord:
        with self._lock:
            self._owned(self._workspaces, workspace_id, tenant_id, "Workspace")
            upload = TabularUploadRecord(
                id=_resource_id("upl"),
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                filename=filename,
                content_type=content_type,
                payload=payload,
                created_at=_now(),
            )
            self._uploads[upload.id] = upload
            return upload

    def get_upload(self, upload_id: str, tenant_id: str) -> TabularUploadRecord:
        with self._lock:
            return self._owned(self._uploads, upload_id, tenant_id, "Tabular upload")

    def create_dataset(
        self,
        upload: TabularUploadRecord,
        table: LoadedTable,
        profile: DataProfile,
        schema_mapping: SchemaMapping,
        recommended_anomaly_features: tuple[str, ...],
    ) -> DatasetRecord:
        with self._lock:
            self._owned(self._uploads, upload.id, upload.tenant_id, "Tabular upload")
            dataset = DatasetRecord(
                id=_resource_id("ds"),
                workspace_id=upload.workspace_id,
                upload_id=upload.id,
                tenant_id=upload.tenant_id,
                table=table,
                profile=profile,
                schema_mapping=schema_mapping,
                recommended_anomaly_features=recommended_anomaly_features,
                mapping_confirmed=False,
                mapping_version=0,
                created_at=_now(),
            )
            self._datasets[dataset.id] = dataset
            return dataset

    def get_dataset(self, dataset_id: str, tenant_id: str) -> DatasetRecord:
        with self._lock:
            return self._owned(self._datasets, dataset_id, tenant_id, "Dataset")

    def confirm_dataset_mapping(
        self,
        dataset_id: str,
        tenant_id: str,
        schema_mapping: SchemaMapping,
        recommended_anomaly_features: tuple[str, ...],
    ) -> DatasetRecord:
        with self._lock:
            dataset = self._owned(self._datasets, dataset_id, tenant_id, "Dataset")
            updated = replace(
                dataset,
                schema_mapping=schema_mapping,
                recommended_anomaly_features=recommended_anomaly_features,
                mapping_confirmed=True,
                mapping_version=dataset.mapping_version + 1,
            )
            self._datasets[dataset_id] = updated
            return updated

    def create_analysis(
        self,
        dataset: DatasetRecord,
        anomaly_contamination: float,
        bundle: AnalysisBundle,
    ) -> AnalysisRecord:
        with self._lock:
            current = self._owned(self._datasets, dataset.id, dataset.tenant_id, "Dataset")
            analysis = AnalysisRecord(
                id=_resource_id("an"),
                workspace_id=current.workspace_id,
                dataset_id=current.id,
                tenant_id=current.tenant_id,
                dataset_mapping_version=current.mapping_version,
                anomaly_contamination=anomaly_contamination,
                bundle=bundle,
                created_at=_now(),
            )
            self._analyses[analysis.id] = analysis
            return analysis

    def get_analysis(self, analysis_id: str, tenant_id: str) -> AnalysisRecord:
        with self._lock:
            return self._owned(self._analyses, analysis_id, tenant_id, "Analysis")

    @staticmethod
    def _owned[RecordT](
        records: dict[str, RecordT],
        resource_id: str,
        tenant_id: str,
        resource_name: str,
    ) -> RecordT:
        record = records.get(resource_id)
        if record is None or getattr(record, "tenant_id", None) != tenant_id:
            raise ResourceNotFoundError(resource_name)
        return record
