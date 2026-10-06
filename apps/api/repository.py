"""Lock-protected process-local resources for the initial API vertical slice."""

from __future__ import annotations

import copy
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import cast
from uuid import uuid4

import pandas as pd

from apps.api.errors import ResourceNotFoundError
from src.agents.report_agent import BusinessReport
from src.analytics.pipeline import AnalysisBundle
from src.charts.chart_builder import ChartSpec
from src.documents.retriever import DocumentRetriever
from src.ingestion.tabular_loader import LoadedTable
from src.memory.session_memory import SessionMemory
from src.orchestration.graph_state import AnswerDiagnostics
from src.profiling.data_profiler import DataProfile
from src.profiling.schema_mapper import SchemaMapping
from src.storage.sqlite_store import StoredTable

_TENANT_DIR = re.compile(r"[a-f0-9]{32}")
_WORKSPACE_DIR = re.compile(r"ws_[a-f0-9]{32}")


@dataclass(frozen=True)
class OrphanSweepResult:
    removed: tuple[tuple[str, str], ...]
    failed: int


WorkspaceLifecycleListener = Callable[[str, "WorkspaceRecord", str], None]
"""Called as ``listener(event, workspace, reason)`` for created/expired/deleted."""


def _resource_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WorkspaceRecord:
    id: str
    tenant_id: str
    authentication_mode: str
    expires_at: datetime
    gemini_configured: bool
    local_only_mode: bool
    consent_notice_version: str | None
    consent_accepted_at: datetime | None
    created_at: datetime
    data_recipients: tuple[str, ...] = ()
    consent_recipients: tuple[str, ...] = ()


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
    stored_table: StoredTable | None
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
    memory: SessionMemory
    created_at: datetime


@dataclass(frozen=True)
class DocumentCollectionRecord:
    id: str
    workspace_id: str
    tenant_id: str
    filenames: tuple[str, ...]
    document_hashes: tuple[str, ...]
    page_count: int
    chunk_count: int
    retriever: DocumentRetriever
    created_at: datetime


@dataclass(frozen=True)
class ConversationRecord:
    id: str
    workspace_id: str
    tenant_id: str
    dataset_id: str | None
    document_collection_id: str | None
    memory: SessionMemory | None
    message_ids: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class MessageRecord:
    id: str
    conversation_id: str
    workspace_id: str
    tenant_id: str
    question: str
    answer: str
    route: str
    sql: str | None
    dataframe: pd.DataFrame | None
    sources: tuple[str, ...]
    created_at: datetime
    request_id: str = "unknown"
    diagnostics: AnswerDiagnostics = AnswerDiagnostics()
    status: str = "complete"
    checkpoint_thread_id: str | None = None


@dataclass(frozen=True)
class ReportRecord:
    id: str
    analysis_id: str
    workspace_id: str
    tenant_id: str
    report: BusinessReport
    chart_specs: tuple[ChartSpec, ...]
    include_charts: bool
    created_at: datetime


@dataclass(frozen=True)
class ExportRecord:
    id: str
    message_id: str
    workspace_id: str
    tenant_id: str
    format: str
    filename: str
    media_type: str
    payload: bytes
    created_at: datetime


class LocalResourceRepository:
    """Initial single-process repository with ownership checks on every lookup.

    The API contract is intentionally independent of this implementation. A
    durable repository can replace it in a later persistence phase without
    changing resource identifiers or response schemas.
    """

    def __init__(
        self,
        storage_root: Path | None = None,
        retention_hours: int = 24,
    ) -> None:
        if retention_hours < 1:
            raise ValueError("retention_hours must be at least 1.")
        self._lock = RLock()
        self.lifecycle_listener: WorkspaceLifecycleListener | None = None
        self.storage_root = (storage_root or Path("data/api")).resolve()
        self.retention_hours = retention_hours
        self._workspaces: dict[str, WorkspaceRecord] = {}
        self._workspace_creation_keys: dict[tuple[str, str], str] = {}
        self._uploads: dict[str, TabularUploadRecord] = {}
        self._datasets: dict[str, DatasetRecord] = {}
        self._analyses: dict[str, AnalysisRecord] = {}
        self._document_collections: dict[str, DocumentCollectionRecord] = {}
        self._conversations: dict[str, ConversationRecord] = {}
        self._messages: dict[str, MessageRecord] = {}
        self._reports: dict[str, ReportRecord] = {}
        self._exports: dict[str, ExportRecord] = {}

    def ready(self) -> bool:
        """True when the workspace storage root exists and accepts writes."""
        try:
            self.storage_root.mkdir(parents=True, exist_ok=True)
            probe = self.storage_root / f".ready-{uuid4().hex}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            return False
        return True

    def close(self) -> None:
        """Release open vector indexes (used at graceful shutdown)."""
        with self._lock:
            for record in self._document_collections.values():
                record.retriever.close()

    def sweep_orphaned_storage(self) -> OrphanSweepResult:
        """Delete workspace directories that no in-memory record owns.

        Resource metadata is process-local, so after a restart every directory
        under the storage root is unreachable by any user. Only run this when a
        single API process owns the storage root; it is opt-in for that reason.
        Symlinks and unexpectedly named entries are never followed or removed.
        """
        removed: list[tuple[str, str]] = []
        failed = 0
        with self._lock:
            if not self.storage_root.is_dir():
                return OrphanSweepResult((), 0)
            known = {(record.tenant_id, record.id) for record in self._workspaces.values()}
            for tenant_dir in sorted(self.storage_root.iterdir()):
                if (
                    tenant_dir.is_symlink()
                    or not tenant_dir.is_dir()
                    or _TENANT_DIR.fullmatch(tenant_dir.name) is None
                ):
                    continue
                for workspace_dir in sorted(tenant_dir.iterdir()):
                    if (
                        workspace_dir.is_symlink()
                        or not workspace_dir.is_dir()
                        or _WORKSPACE_DIR.fullmatch(workspace_dir.name) is None
                        or (tenant_dir.name, workspace_dir.name) in known
                        or workspace_dir.resolve().parent != tenant_dir.resolve()
                    ):
                        continue
                    try:
                        shutil.rmtree(workspace_dir)
                    except OSError:
                        failed += 1
                        continue
                    removed.append((tenant_dir.name, workspace_dir.name))
        return OrphanSweepResult(tuple(removed), failed)

    def create_workspace(
        self,
        tenant_id: str,
        authentication_mode: str,
        idempotency_key: str | None,
        *,
        gemini_configured: bool = False,
        data_recipients: tuple[str, ...] = (),
        local_only_mode: bool = False,
    ) -> WorkspaceRecord:
        with self._lock:
            self._purge_expired_unlocked()
            if idempotency_key is not None:
                existing_id = self._workspace_creation_keys.get((tenant_id, idempotency_key))
                if existing_id is not None:
                    existing = self._workspaces.get(existing_id)
                    if existing is not None and existing.expires_at > _now():
                        return self._touch_workspace_unlocked(existing)
            workspace = WorkspaceRecord(
                id=_resource_id("ws"),
                tenant_id=tenant_id,
                authentication_mode=authentication_mode,
                expires_at=_now() + timedelta(hours=self.retention_hours),
                gemini_configured=gemini_configured,
                data_recipients=data_recipients,
                local_only_mode=local_only_mode,
                consent_notice_version=None,
                consent_accepted_at=None,
                created_at=_now(),
            )
            self._workspaces[workspace.id] = workspace
            if idempotency_key is not None:
                self._workspace_creation_keys[(tenant_id, idempotency_key)] = workspace.id
            self._notify("created", workspace, "requested")
            return workspace

    def get_workspace(self, workspace_id: str, tenant_id: str) -> WorkspaceRecord:
        with self._lock:
            return self._owned(self._workspaces, workspace_id, tenant_id, "Workspace")

    def set_consent(
        self,
        workspace_id: str,
        tenant_id: str,
        notice_version: str,
        recipients: tuple[str, ...] = (),
    ) -> WorkspaceRecord:
        with self._lock:
            workspace = self._owned(self._workspaces, workspace_id, tenant_id, "Workspace")
            updated = replace(
                workspace,
                consent_notice_version=notice_version,
                consent_accepted_at=_now(),
                consent_recipients=recipients,
            )
            self._workspaces[workspace_id] = updated
            return self._touch_workspace_unlocked(updated)

    def workspace_dir(self, workspace_id: str, tenant_id: str) -> Path:
        workspace = self.get_workspace(workspace_id, tenant_id)
        target = (self.storage_root / workspace.tenant_id / workspace.id).resolve()
        if not target.is_relative_to(self.storage_root):
            raise ValueError("Refusing to use storage outside the API data directory.")
        target.mkdir(parents=True, exist_ok=True)
        return target

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
                stored_table=None,
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
        stored_table: StoredTable,
    ) -> DatasetRecord:
        with self._lock:
            dataset = self._owned(self._datasets, dataset_id, tenant_id, "Dataset")
            updated = replace(
                dataset,
                schema_mapping=schema_mapping,
                recommended_anomaly_features=recommended_anomaly_features,
                mapping_confirmed=True,
                mapping_version=dataset.mapping_version + 1,
                stored_table=stored_table,
            )
            self._datasets[dataset_id] = updated
            return updated

    def create_analysis(
        self,
        dataset: DatasetRecord,
        anomaly_contamination: float,
        bundle: AnalysisBundle,
        memory: SessionMemory,
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
                memory=memory,
                created_at=_now(),
            )
            self._analyses[analysis.id] = analysis
            return analysis

    def get_analysis(self, analysis_id: str, tenant_id: str) -> AnalysisRecord:
        with self._lock:
            return self._owned(self._analyses, analysis_id, tenant_id, "Analysis")

    def latest_analysis_for_dataset(
        self,
        dataset_id: str,
        tenant_id: str,
    ) -> AnalysisRecord | None:
        with self._lock:
            self._owned(self._datasets, dataset_id, tenant_id, "Dataset")
            matches = [
                record
                for record in self._analyses.values()
                if record.dataset_id == dataset_id and record.tenant_id == tenant_id
            ]
            return max(matches, key=lambda record: record.created_at) if matches else None

    def create_document_collection(
        self,
        *,
        workspace_id: str,
        tenant_id: str,
        filenames: tuple[str, ...],
        document_hashes: tuple[str, ...],
        page_count: int,
        chunk_count: int,
        retriever: DocumentRetriever,
    ) -> DocumentCollectionRecord:
        with self._lock:
            self._owned(self._workspaces, workspace_id, tenant_id, "Workspace")
            previous_ids = [
                key
                for key, existing in self._document_collections.items()
                if existing.workspace_id == workspace_id
            ]
            for previous_id in previous_ids:
                self._document_collections.pop(previous_id).retriever.close()
            record = DocumentCollectionRecord(
                id=_resource_id("docs"),
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                filenames=filenames,
                document_hashes=document_hashes,
                page_count=page_count,
                chunk_count=chunk_count,
                retriever=retriever,
                created_at=_now(),
            )
            self._document_collections[record.id] = record
            return record

    def get_document_collection(
        self,
        collection_id: str,
        tenant_id: str,
    ) -> DocumentCollectionRecord:
        with self._lock:
            return self._owned(
                self._document_collections,
                collection_id,
                tenant_id,
                "Document collection",
            )

    def latest_document_collection_for_workspace(
        self,
        workspace_id: str,
        tenant_id: str,
    ) -> DocumentCollectionRecord | None:
        with self._lock:
            self._owned(self._workspaces, workspace_id, tenant_id, "Workspace")
            matches = [
                record
                for record in self._document_collections.values()
                if record.workspace_id == workspace_id and record.tenant_id == tenant_id
            ]
            return max(matches, key=lambda record: record.created_at) if matches else None

    def create_conversation(
        self,
        *,
        workspace_id: str,
        tenant_id: str,
        dataset_id: str | None,
        document_collection_id: str | None,
    ) -> ConversationRecord:
        with self._lock:
            self._owned(self._workspaces, workspace_id, tenant_id, "Workspace")
            memory = None
            if dataset_id is not None:
                dataset = self._owned(self._datasets, dataset_id, tenant_id, "Dataset")
                if dataset.workspace_id != workspace_id:
                    raise ResourceNotFoundError("Dataset")
                latest = self.latest_analysis_for_dataset(dataset_id, tenant_id)
                memory = copy.deepcopy(latest.memory) if latest is not None else None
            if document_collection_id is not None:
                documents = self._owned(
                    self._document_collections,
                    document_collection_id,
                    tenant_id,
                    "Document collection",
                )
                if documents.workspace_id != workspace_id:
                    raise ResourceNotFoundError("Document collection")
                if memory is not None:
                    memory.document_summary = [
                        f"Indexed documents: {len(documents.filenames)}",
                        f"Document chunks: {documents.chunk_count}",
                        "Files: " + ", ".join(documents.filenames),
                    ]
            if dataset_id is None and document_collection_id is None and memory is None:
                raise ValueError("A conversation requires a dataset or document collection.")
            record = ConversationRecord(
                id=_resource_id("conv"),
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                dataset_id=dataset_id,
                document_collection_id=document_collection_id,
                memory=memory,
                message_ids=(),
                created_at=_now(),
            )
            self._conversations[record.id] = record
            return record

    def get_conversation(self, conversation_id: str, tenant_id: str) -> ConversationRecord:
        with self._lock:
            return self._owned(
                self._conversations,
                conversation_id,
                tenant_id,
                "Conversation",
            )

    def add_message(
        self,
        *,
        message_id: str | None = None,
        conversation_id: str,
        tenant_id: str,
        question: str,
        answer: str,
        route: str,
        sql: str | None,
        dataframe: pd.DataFrame | None,
        sources: tuple[str, ...],
        memory: SessionMemory | None,
        max_messages: int,
        max_dataframes: int,
        request_id: str = "unknown",
        diagnostics: AnswerDiagnostics | None = None,
        status: str = "complete",
        checkpoint_thread_id: str | None = None,
    ) -> MessageRecord:
        with self._lock:
            conversation = self._owned(
                self._conversations,
                conversation_id,
                tenant_id,
                "Conversation",
            )
            message = MessageRecord(
                id=message_id or _resource_id("msg"),
                conversation_id=conversation.id,
                workspace_id=conversation.workspace_id,
                tenant_id=tenant_id,
                question=question,
                answer=answer,
                route=route,
                sql=sql,
                dataframe=dataframe.copy() if dataframe is not None else None,
                sources=sources,
                created_at=_now(),
                request_id=request_id,
                diagnostics=diagnostics or AnswerDiagnostics(),
                status=status,
                checkpoint_thread_id=checkpoint_thread_id,
            )
            self._messages[message.id] = message
            all_message_ids = (*conversation.message_ids, message.id)
            message_ids = all_message_ids[-max_messages:]
            dropped_message_ids = set(all_message_ids) - set(message_ids)
            for dropped_id in dropped_message_ids:
                self._messages.pop(dropped_id, None)
                for export_id in [
                    export_id
                    for export_id, export in self._exports.items()
                    if export.message_id == dropped_id
                ]:
                    self._exports.pop(export_id, None)
            dataframe_message_ids = [
                message_id
                for message_id in message_ids
                if self._messages[message_id].dataframe is not None
            ]
            retained_with_frames = (
                set(dataframe_message_ids[-max_dataframes:]) if max_dataframes else set()
            )
            for message_id in message_ids:
                existing = self._messages[message_id]
                if existing.dataframe is not None and message_id not in retained_with_frames:
                    self._messages[message_id] = replace(existing, dataframe=None)
            updated = replace(
                conversation,
                memory=memory,
                message_ids=tuple(message_ids),
            )
            self._conversations[conversation.id] = updated
            return message

    def resolve_message(
        self,
        *,
        message_id: str,
        tenant_id: str,
        answer: str,
        route: str,
        sql: str | None,
        dataframe: pd.DataFrame | None,
        sources: tuple[str, ...],
        diagnostics: AnswerDiagnostics,
        status: str,
        memory: SessionMemory | None,
    ) -> MessageRecord:
        """Replace a pending message after a tenant-checked approval decision."""
        with self._lock:
            message = self._owned(self._messages, message_id, tenant_id, "Message")
            if message.status != "pending_approval":
                raise ValueError("This message no longer has a pending approval.")
            updated = replace(
                message,
                answer=answer,
                route=route,
                sql=sql,
                dataframe=dataframe.copy() if dataframe is not None else None,
                sources=sources,
                diagnostics=diagnostics,
                status=status,
                checkpoint_thread_id=(
                    message.checkpoint_thread_id if status == "pending_approval" else None
                ),
            )
            self._messages[message.id] = updated
            conversation = self._owned(
                self._conversations,
                message.conversation_id,
                tenant_id,
                "Conversation",
            )
            self._conversations[conversation.id] = replace(conversation, memory=memory)
            return updated

    def list_messages(self, conversation_id: str, tenant_id: str) -> list[MessageRecord]:
        conversation = self.get_conversation(conversation_id, tenant_id)
        with self._lock:
            return [self._messages[message_id] for message_id in conversation.message_ids]

    def list_messages_page(
        self,
        conversation_id: str,
        tenant_id: str,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[list[MessageRecord], bool]:
        """Return a stable page of retained messages plus a next-page flag."""
        if limit < 1:
            raise ValueError("limit must be positive.")
        conversation = self.get_conversation(conversation_id, tenant_id)
        with self._lock:
            records = [self._messages[message_id] for message_id in conversation.message_ids]
            records.sort(key=lambda record: (record.created_at, record.id))
            if after is not None:
                records = [record for record in records if (record.created_at, record.id) > after]
            page = records[: limit + 1]
            return page[:limit], len(page) > limit

    def get_message(self, message_id: str, tenant_id: str) -> MessageRecord:
        with self._lock:
            return self._owned(self._messages, message_id, tenant_id, "Message")

    def create_report(
        self,
        *,
        analysis: AnalysisRecord,
        tenant_id: str,
        include_charts: bool,
    ) -> ReportRecord:
        with self._lock:
            self._owned(self._analyses, analysis.id, tenant_id, "Analysis")
            record = ReportRecord(
                id=_resource_id("report"),
                analysis_id=analysis.id,
                workspace_id=analysis.workspace_id,
                tenant_id=tenant_id,
                report=analysis.bundle.business_report,
                chart_specs=tuple(analysis.bundle.chart_specs),
                include_charts=include_charts,
                created_at=_now(),
            )
            self._reports[record.id] = record
            return record

    def get_report(self, report_id: str, tenant_id: str) -> ReportRecord:
        with self._lock:
            return self._owned(self._reports, report_id, tenant_id, "Report")

    def create_export(
        self,
        *,
        message: MessageRecord,
        tenant_id: str,
        format: str,
        filename: str,
        media_type: str,
        payload: bytes,
    ) -> ExportRecord:
        with self._lock:
            self._owned(self._messages, message.id, tenant_id, "Message")
            record = ExportRecord(
                id=_resource_id("export"),
                message_id=message.id,
                workspace_id=message.workspace_id,
                tenant_id=tenant_id,
                format=format,
                filename=filename,
                media_type=media_type,
                payload=payload,
                created_at=_now(),
            )
            self._exports[record.id] = record
            return record

    def get_export(self, export_id: str, tenant_id: str) -> ExportRecord:
        with self._lock:
            return self._owned(self._exports, export_id, tenant_id, "Export")

    def delete_workspace(self, workspace_id: str, tenant_id: str) -> None:
        with self._lock:
            workspace = self._owned(self._workspaces, workspace_id, tenant_id, "Workspace")
            self._delete_workspace_unlocked(workspace, "user_requested")

    def purge_expired(self) -> int:
        """Delete every expired workspace now; returns how many were removed.

        Expiry is otherwise lazy (checked when a resource is touched), so this
        gives retention a deterministic trigger and each removal an audit event.
        """
        with self._lock:
            return self._purge_expired_unlocked()

    def _purge_expired_unlocked(self) -> int:
        expired = [
            workspace for workspace in self._workspaces.values() if workspace.expires_at <= _now()
        ]
        for workspace in expired:
            self._delete_workspace_unlocked(workspace, "retention_expired")
        return len(expired)

    def _notify(self, event: str, workspace: WorkspaceRecord, reason: str) -> None:
        listener = self.lifecycle_listener
        if listener is not None:
            listener(event, workspace, reason)

    def _touch_workspace_unlocked(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        updated = replace(
            workspace,
            expires_at=_now() + timedelta(hours=self.retention_hours),
        )
        self._workspaces[workspace.id] = updated
        return updated

    def _delete_workspace_unlocked(
        self,
        workspace: WorkspaceRecord,
        reason: str = "retention_expired",
    ) -> None:
        collection_ids = [
            key
            for key, record in self._document_collections.items()
            if record.workspace_id == workspace.id
        ]
        for collection_id in collection_ids:
            retriever = self._document_collections.pop(collection_id).retriever
            retriever.purge()  # a database-backed index is not removed with the workspace directory
            retriever.close()

        for records in (
            self._uploads,
            self._datasets,
            self._analyses,
            self._conversations,
            self._messages,
            self._reports,
            self._exports,
        ):
            for key in [
                key
                for key, record in records.items()
                if getattr(record, "workspace_id", None) == workspace.id
            ]:
                records.pop(key, None)

        self._workspaces.pop(workspace.id, None)
        for creation_key, value in list(self._workspace_creation_keys.items()):
            if value == workspace.id:
                self._workspace_creation_keys.pop(creation_key)

        target = (self.storage_root / workspace.tenant_id / workspace.id).resolve()
        if target.is_relative_to(self.storage_root) and target.exists():
            shutil.rmtree(target)
        self._notify("expired" if reason == "retention_expired" else "deleted", workspace, reason)

    def _owned[RecordT](
        self,
        records: dict[str, RecordT],
        resource_id: str,
        tenant_id: str,
        resource_name: str,
    ) -> RecordT:
        record = records.get(resource_id)
        if record is None or getattr(record, "tenant_id", None) != tenant_id:
            raise ResourceNotFoundError(resource_name)
        workspace: WorkspaceRecord
        if isinstance(record, WorkspaceRecord):
            workspace = record
        else:
            workspace_id = getattr(record, "workspace_id", None)
            if not isinstance(workspace_id, str):
                raise ResourceNotFoundError(resource_name)
            resource_workspace = self._workspaces.get(workspace_id)
            if resource_workspace is None or resource_workspace.tenant_id != tenant_id:
                raise ResourceNotFoundError(resource_name)
            workspace = resource_workspace
        if workspace.expires_at <= _now():
            self._delete_workspace_unlocked(workspace, "retention_expired")
            raise ResourceNotFoundError(resource_name)
        touched = self._touch_workspace_unlocked(workspace)
        if isinstance(record, WorkspaceRecord):
            return cast(RecordT, touched)
        return record
