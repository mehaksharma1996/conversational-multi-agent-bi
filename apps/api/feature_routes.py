"""Feature-parity API routes for documents, conversation, reports, and exports."""

from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from apps.api.approvals import ApprovalCheckpoints
from apps.api.authorization import Capability, requires
from apps.api.dependencies import (
    get_api_settings,
    get_approval_checkpoints,
    get_document_service,
    get_identity,
    get_llm_client,
    get_observability,
    get_rate_limiter,
    get_repository,
)
from apps.api.errors import STANDARD_ERROR_RESPONSES, ApiError, ResourceConflictError
from apps.api.http_semantics import DOWNLOAD_SEMANTICS, NON_IDEMPOTENT_CREATE_SEMANTICS
from apps.api.models import (
    ConsentResponse,
    ConsentUpdateRequest,
    ConversationCreateRequest,
    ConversationResponse,
    DocumentCollectionResponse,
    ExportCreateRequest,
    ExportResponse,
    MessageApprovalRequest,
    MessageCreateRequest,
    MessageListResponse,
    MessageResponse,
    ReportCreateRequest,
    ReportResponse,
)
from apps.api.observability import ApiObservability
from apps.api.pagination import decode_cursor, encode_cursor
from apps.api.rate_limit import InMemoryRateLimiter, enforce_rate_limit
from apps.api.repository import LocalResourceRepository
from apps.api.serializers import (
    conversation_response,
    document_collection_response,
    export_response,
    message_list_response,
    message_response,
    report_response,
)
from config.settings import Settings
from packages.connectors import IdentityContext
from packages.observability import current_request_id, error_category
from packages.retrieval import DocumentApplicationService, DocumentPayload, IndexDocumentsCommand
from src.agents.rag_agent import RAGAgentError
from src.agents.report_agent import report_to_markdown
from src.agents.sql_agent import SQLAgentError
from src.documents.embedding import EmbeddingError
from src.documents.pgvector_store import psycopg_connection_factory
from src.ingestion.pdf_loader import PDFLoadError
from src.llm.base import LLMClient, LLMConfigurationError, LLMGenerationError
from src.memory.session_memory import remember_question
from src.orchestration.langgraph_orchestrator import OrchestratorResult, QuestionOrchestrator
from src.reporting.pdf_report import build_report_pdf
from src.storage.query_executor import QueryTimeoutError, UnsafeQueryError
from src.utils.spreadsheet_safety import sanitize_dataframe_for_export

router = APIRouter(prefix="/api/v1")

IdentityDependency = Annotated[IdentityContext, Depends(get_identity)]
RepositoryDependency = Annotated[LocalResourceRepository, Depends(get_repository)]
SettingsDependency = Annotated[Settings, Depends(get_api_settings)]
DocumentServiceDependency = Annotated[DocumentApplicationService, Depends(get_document_service)]
LLMDependency = Annotated[LLMClient, Depends(get_llm_client)]
ObservabilityDependency = Annotated[ApiObservability, Depends(get_observability)]
ApprovalCheckpointsDependency = Annotated[ApprovalCheckpoints, Depends(get_approval_checkpoints)]
RateLimiterDependency = Annotated[InMemoryRateLimiter, Depends(get_rate_limiter)]

DOWNLOAD_RESPONSE_HEADERS = {
    "Accept-Ranges": {
        "description": "Always `none`; partial byte ranges are not supported.",
        "schema": {"type": "string"},
    },
    "Cache-Control": {
        "description": "Always `private, no-store`.",
        "schema": {"type": "string"},
    },
    "Content-Disposition": {
        "description": "Attachment filename for the representation.",
        "schema": {"type": "string"},
    },
    "Content-Length": {
        "description": "Exact representation size in bytes.",
        "schema": {"type": "integer", "minimum": 0},
    },
}

REPORT_CONTENT_RESPONSES = {
    **STANDARD_ERROR_RESPONSES,
    200: {
        "description": "Generated report content",
        "content": {
            "text/markdown": {"schema": {"type": "string"}},
            "application/pdf": {"schema": {"type": "string", "format": "binary"}},
        },
        "headers": DOWNLOAD_RESPONSE_HEADERS,
    },
}
EXPORT_CONTENT_RESPONSES = {
    **STANDARD_ERROR_RESPONSES,
    200: {
        "description": "Sanitized tabular query result",
        "content": {
            "text/csv": {"schema": {"type": "string"}},
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                "schema": {"type": "string", "format": "binary"}
            },
        },
        "headers": DOWNLOAD_RESPONSE_HEADERS,
    },
}


@router.put(
    "/workspaces/{workspace_id}/consent",
    dependencies=[requires(Capability.WORKSPACE_ADMIN)],
    response_model=ConsentResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["workspaces"],
)
def accept_gemini_consent(
    workspace_id: str,
    command: ConsentUpdateRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    observability: ObservabilityDependency,
    settings: SettingsDependency,
) -> ConsentResponse:
    # The recipients are read from server configuration, never from the request.
    workspace = repository.set_consent(
        workspace_id,
        identity.tenant_id,
        command.notice_version,
        recipients=settings.hosted_recipients(),
    )
    observability.audit(
        "consent.accepted",
        identity,
        resource_id=workspace.id,
        notice_version=workspace.consent_notice_version,
        consent_required=workspace.gemini_configured,
    )
    return ConsentResponse(
        workspace_id=workspace.id,
        required=workspace.gemini_configured,
        accepted=True,
        notice_version=workspace.consent_notice_version,
        accepted_at=workspace.consent_accepted_at,
        data_recipients=list(workspace.consent_recipients),
    )


@router.delete(
    "/workspaces/{workspace_id}",
    dependencies=[requires(Capability.WORKSPACE_ADMIN)],
    status_code=status.HTTP_204_NO_CONTENT,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["workspaces"],
)
def delete_workspace(
    workspace_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
) -> Response:
    repository.delete_workspace(workspace_id, identity.tenant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/workspaces/{workspace_id}/document-collections",
    dependencies=[requires(Capability.DATA_WRITE)],
    response_model=DocumentCollectionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["documents"],
    description=NON_IDEMPOTENT_CREATE_SEMANTICS,
)
async def create_document_collection(
    workspace_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    settings: SettingsDependency,
    service: DocumentServiceDependency,
    observability: ObservabilityDependency,
    rate_limiter: RateLimiterDependency,
    files: Annotated[list[UploadFile], File(description="One or more PDF documents")],
) -> DocumentCollectionResponse:
    repository.get_workspace(workspace_id, identity.tenant_id)
    enforce_rate_limit(
        rate_limiter,
        operation="document_index",
        tenant_id=identity.tenant_id,
        workspace_id=workspace_id,
        limit=settings.rate_limit_document_indexes,
        window_seconds=settings.rate_limit_window_seconds,
    )
    payloads: list[DocumentPayload] = []
    total_bytes = 0
    try:
        for file in files:
            filename = _safe_pdf_filename(file.filename)
            payload = await file.read(settings.max_pdf_upload_bytes + 1)
            if len(payload) > settings.max_pdf_upload_bytes:
                limit_mb = settings.max_pdf_upload_bytes / (1024 * 1024)
                raise ApiError(
                    413,
                    "pdf_upload_too_large",
                    f"Each PDF is limited to {limit_mb:g} MB: {filename}.",
                )
            total_bytes += len(payload)
            if total_bytes > settings.max_total_pdf_bytes:
                limit_mb = settings.max_total_pdf_bytes / (1024 * 1024)
                raise ApiError(
                    413,
                    "combined_pdf_upload_too_large",
                    f"The combined PDF upload is limited to {limit_mb:g} MB.",
                )
            payloads.append(DocumentPayload(filename=filename, payload=payload))
    finally:
        for file in files:
            await file.close()

    try:
        with observability.operation(
            "documents.index",
            identity,
            size_bytes=total_bytes,
        ) as operation:
            # Extraction, embedding, and index writes are blocking and can run for many seconds;
            # keep them off the event loop so health checks and other requests stay responsive.
            indexed = await run_in_threadpool(
                service.index,
                IndexDocumentsCommand(
                    documents=tuple(payloads),
                    persist_dir=repository.workspace_dir(workspace_id, identity.tenant_id)
                    / "vectorstore",
                    embedding_model=settings.embedding_model,
                    max_pages=settings.max_pdf_pages,
                    max_chunks=settings.max_document_chunks,
                    retrieval_top_k=settings.retrieval_top_k,
                    retrieval_max_distance=settings.retrieval_max_distance,
                    retrieval_hybrid=settings.retrieval_hybrid,
                    index_backend=settings.document_index_backend,
                    index_scope=f"{identity.tenant_id}/{workspace_id}",
                    pgvector_connect=(
                        psycopg_connection_factory(str(settings.postgres_dsn))
                        if settings.document_index_backend == "pgvector"
                        else None
                    ),
                    pgvector_auto_migrate=settings.postgres_auto_migrate,
                    cache_tenant_id=identity.tenant_id,
                    cache_workspace_id=workspace_id,
                ),
            )
            operation.set(
                document_count=len(indexed.filenames),
                page_count=indexed.page_count,
                chunk_count=indexed.chunk_count,
            )
    except (PDFLoadError, EmbeddingError, ValueError) as exc:
        raise ApiError(422, "invalid_document_collection", str(exc)) from exc

    record = repository.create_document_collection(
        workspace_id=workspace_id,
        tenant_id=identity.tenant_id,
        filenames=indexed.filenames,
        document_hashes=indexed.document_hashes,
        page_count=indexed.page_count,
        chunk_count=indexed.chunk_count,
        retriever=indexed.retriever,
    )
    observability.audit(
        "documents.indexed",
        identity,
        resource_id=record.id,
        document_count=len(record.filenames),
        page_count=record.page_count,
        chunk_count=record.chunk_count,
    )
    return document_collection_response(record)


@router.get(
    "/document-collections/{collection_id}",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=DocumentCollectionResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["documents"],
)
def get_document_collection(
    collection_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
) -> DocumentCollectionResponse:
    return document_collection_response(
        repository.get_document_collection(collection_id, identity.tenant_id)
    )


@router.post(
    "/workspaces/{workspace_id}/conversations",
    dependencies=[requires(Capability.ANALYSIS_RUN)],
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["conversations"],
    description=NON_IDEMPOTENT_CREATE_SEMANTICS,
)
def create_conversation(
    workspace_id: str,
    command: ConversationCreateRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    observability: ObservabilityDependency,
) -> ConversationResponse:
    try:
        record = repository.create_conversation(
            workspace_id=workspace_id,
            tenant_id=identity.tenant_id,
            dataset_id=command.dataset_id,
            document_collection_id=command.document_collection_id,
        )
    except ValueError as exc:
        raise ApiError(422, "conversation_context_required", str(exc)) from exc
    observability.audit("conversation.created", identity, resource_id=record.id)
    return conversation_response(record)


@router.get(
    "/conversations/{conversation_id}",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=ConversationResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["conversations"],
)
def get_conversation(
    conversation_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
) -> ConversationResponse:
    return conversation_response(repository.get_conversation(conversation_id, identity.tenant_id))


@router.post(
    "/conversations/{conversation_id}/messages",
    dependencies=[requires(Capability.ANALYSIS_RUN)],
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["conversations"],
    description=NON_IDEMPOTENT_CREATE_SEMANTICS,
)
def create_message(
    conversation_id: str,
    command: MessageCreateRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    settings: SettingsDependency,
    llm_client: LLMDependency,
    observability: ObservabilityDependency,
    approval_checkpoints: ApprovalCheckpointsDependency,
    rate_limiter: RateLimiterDependency,
) -> MessageResponse:
    conversation = repository.get_conversation(conversation_id, identity.tenant_id)
    enforce_rate_limit(
        rate_limiter,
        operation="message",
        tenant_id=identity.tenant_id,
        workspace_id=conversation.workspace_id,
        limit=settings.rate_limit_messages,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if any(
        message.status == "pending_approval"
        for message in repository.list_messages(conversation.id, identity.tenant_id)
    ):
        raise ResourceConflictError(
            "sql_approval_already_pending",
            "Resolve the pending SQL approval before asking another question.",
        )
    workspace = repository.get_workspace(conversation.workspace_id, identity.tenant_id)
    recipients = settings.hosted_recipients()
    consented = workspace.consent_accepted_at is not None and set(recipients) <= set(
        workspace.consent_recipients
    )
    if recipients and not consented:
        raise ResourceConflictError(
            "gemini_consent_required",
            "Accept the Gemini data-sharing notice before asking model-backed questions.",
        )

    dataset = (
        repository.get_dataset(conversation.dataset_id, identity.tenant_id)
        if conversation.dataset_id is not None
        else None
    )
    documents = (
        repository.get_document_collection(
            conversation.document_collection_id,
            identity.tenant_id,
        )
        if conversation.document_collection_id is not None
        else None
    )
    if dataset is not None and dataset.stored_table is None:
        raise ResourceConflictError(
            "schema_mapping_not_confirmed",
            "Confirm the dataset schema mapping before asking data questions.",
        )

    message_id = f"msg_{uuid4().hex}"
    thread_id = f"{identity.tenant_id}:{conversation.workspace_id}:{message_id}"
    orchestrator = QuestionOrchestrator(
        llm_client=llm_client,
        stored_table=dataset.stored_table if dataset is not None else None,
        document_retriever=documents.retriever if documents is not None else None,
        session_memory=conversation.memory,
        debug_log_raw_content=settings.debug_log_raw_content,
        checkpointer=approval_checkpoints.saver,
    )
    provider_used = bool(getattr(llm_client, "configured", False))
    llm_attributes = {
        "llm_provider": getattr(llm_client, "provider", None),
        "llm_model": getattr(llm_client, "model", None),
    }
    try:
        with observability.operation(
            "agent.answer",
            identity,
            question_length=len(command.question),
            **llm_attributes,
        ) as operation:
            try:
                result = orchestrator.answer(
                    command.question,
                    require_sql_approval=command.require_sql_approval,
                    thread_id=thread_id,
                )
            except RAGAgentError as exc:
                operation.set(
                    retrieval_candidates=exc.candidates_considered,
                    retrieval_accepted=0,
                    retrieval_rejected_distance=exc.candidates_rejected_by_distance,
                )
                raise
            finally:
                operation.set(route=orchestrator.last_route)
            operation.set(**_answer_attributes(result))
    except (
        LLMConfigurationError,
        LLMGenerationError,
        SQLAgentError,
        RAGAgentError,
        UnsafeQueryError,
        QueryTimeoutError,
        ValueError,
    ) as exc:
        approval_checkpoints.discard(conversation.workspace_id, thread_id)
        observability.audit(
            "agent.route_executed",
            identity,
            resource_id=conversation.id,
            route=orchestrator.last_route,
            outcome="failure",
            error_category=error_category(exc),
            provider_used=provider_used,
            **llm_attributes,
        )
        raise ApiError(422, "question_could_not_be_answered_safely", str(exc)) from exc

    if result.status == "pending_approval":
        approval_checkpoints.register(conversation.workspace_id, thread_id)
    else:
        approval_checkpoints.discard(conversation.workspace_id, thread_id)
    memory = conversation.memory
    if memory is not None and result.status == "complete":
        memory = remember_question(memory, command.question, result.sql)
    record = repository.add_message(
        message_id=message_id,
        conversation_id=conversation.id,
        tenant_id=identity.tenant_id,
        question=command.question,
        answer=result.answer,
        route=result.route,
        sql=result.sql,
        dataframe=result.dataframe,
        sources=tuple(result.sources or ()),
        memory=memory,
        max_messages=settings.max_chat_messages,
        max_dataframes=settings.max_chat_dataframes_retained,
        request_id=current_request_id() or "unknown",
        diagnostics=result.diagnostics,
        status=result.status,
        checkpoint_thread_id=(thread_id if result.status == "pending_approval" else None),
    )
    observability.audit(
        "agent.route_executed",
        identity,
        resource_id=record.id,
        route=result.route,
        outcome="success",
        provider_used=provider_used,
        has_sql=result.sql is not None,
        result_row_count=len(result.dataframe) if result.dataframe is not None else 0,
        source_count=len(result.sources or ()),
        grounding_status=result.diagnostics.grounding_status,
        fallback_used=result.diagnostics.llm_fallbacks > 0,
        llm_provider=result.llm_provider_used or llm_attributes["llm_provider"],
        llm_model=result.llm_model_used or llm_attributes["llm_model"],
    )
    return message_response(record)


@router.post(
    "/messages/{message_id}/approval",
    dependencies=[requires(Capability.ANALYSIS_RUN)],
    response_model=MessageResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["conversations"],
)
def decide_sql_approval(
    message_id: str,
    command: MessageApprovalRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    settings: SettingsDependency,
    llm_client: LLMDependency,
    observability: ObservabilityDependency,
    approval_checkpoints: ApprovalCheckpointsDependency,
) -> MessageResponse:
    """Resume one tenant-owned SQL approval without accepting a client thread ID."""
    message = repository.get_message(message_id, identity.tenant_id)
    if message.status != "pending_approval" or message.checkpoint_thread_id is None:
        raise ResourceConflictError(
            "sql_approval_not_pending",
            "This message no longer has a pending SQL approval.",
        )

    conversation = repository.get_conversation(
        message.conversation_id,
        identity.tenant_id,
    )
    dataset = (
        repository.get_dataset(conversation.dataset_id, identity.tenant_id)
        if conversation.dataset_id is not None
        else None
    )
    documents = (
        repository.get_document_collection(
            conversation.document_collection_id,
            identity.tenant_id,
        )
        if conversation.document_collection_id is not None
        else None
    )
    orchestrator = QuestionOrchestrator(
        llm_client=llm_client,
        stored_table=dataset.stored_table if dataset is not None else None,
        document_retriever=documents.retriever if documents is not None else None,
        session_memory=conversation.memory,
        debug_log_raw_content=settings.debug_log_raw_content,
        checkpointer=approval_checkpoints.saver,
    )
    sql_edited = command.sql is not None and command.sql != message.sql
    audit_action = "agent.sql_approved" if command.decision == "approve" else "agent.sql_rejected"
    try:
        result = orchestrator.resume_approval(
            message.checkpoint_thread_id,
            decision=command.decision,
            sql=command.sql,
        )
    except (
        LLMConfigurationError,
        LLMGenerationError,
        SQLAgentError,
        RAGAgentError,
        UnsafeQueryError,
        QueryTimeoutError,
        ValueError,
    ) as exc:
        repository.resolve_message(
            message_id=message.id,
            tenant_id=identity.tenant_id,
            answer="SQL approval failed safely. No table query result was retained.",
            route=message.route,
            sql=message.sql,
            dataframe=None,
            sources=message.sources,
            diagnostics=message.diagnostics,
            status="rejected",
            memory=conversation.memory,
        )
        approval_checkpoints.discard(
            conversation.workspace_id,
            message.checkpoint_thread_id,
        )
        observability.audit(
            audit_action,
            identity,
            resource_id=message.id,
            outcome="failure",
            error_category=error_category(exc),
            sql_edited=sql_edited,
        )
        raise ApiError(422, "sql_approval_could_not_be_applied_safely", str(exc)) from exc

    if result.status not in {"complete", "pending_approval", "rejected"}:
        raise ApiError(
            500,
            "sql_approval_incomplete",
            "The SQL approval did not reach a terminal state.",
        )
    memory = conversation.memory
    if memory is not None and result.status == "complete":
        memory = remember_question(memory, message.question, result.sql)
    record = repository.resolve_message(
        message_id=message.id,
        tenant_id=identity.tenant_id,
        answer=result.answer,
        route=result.route,
        sql=result.sql,
        dataframe=result.dataframe,
        sources=tuple(result.sources or ()),
        diagnostics=result.diagnostics,
        status=result.status,
        memory=memory,
    )
    if result.status == "pending_approval":
        approval_checkpoints.register(
            conversation.workspace_id,
            message.checkpoint_thread_id,
        )
    else:
        approval_checkpoints.discard(
            conversation.workspace_id,
            message.checkpoint_thread_id,
        )
    observability.audit(
        audit_action,
        identity,
        resource_id=record.id,
        outcome="success",
        sql_edited=sql_edited,
    )
    return message_response(record)


@router.get(
    "/conversations/{conversation_id}/messages",
    dependencies=[requires(Capability.WORKSPACE_READ)],
    response_model=MessageListResponse,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["conversations"],
)
def list_messages(
    conversation_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> MessageListResponse:
    after = decode_cursor(cursor) if cursor is not None else None
    records, has_more = repository.list_messages_page(
        conversation_id,
        identity.tenant_id,
        limit=limit,
        after=after,
    )
    next_cursor = (
        encode_cursor(records[-1].created_at, records[-1].id) if has_more and records else None
    )
    return message_list_response(conversation_id, records, next_cursor)


@router.post(
    "/analyses/{analysis_id}/reports",
    dependencies=[requires(Capability.REPORT_EXPORT)],
    response_model=ReportResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["reports"],
    description=NON_IDEMPOTENT_CREATE_SEMANTICS,
)
def create_report(
    analysis_id: str,
    command: ReportCreateRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    observability: ObservabilityDependency,
) -> ReportResponse:
    analysis = repository.get_analysis(analysis_id, identity.tenant_id)
    record = repository.create_report(
        analysis=analysis,
        tenant_id=identity.tenant_id,
        include_charts=command.include_charts,
    )
    observability.audit(
        "report.generated",
        identity,
        resource_id=record.id,
        include_charts=record.include_charts,
    )
    return report_response(record)


@router.get(
    "/reports/{report_id}/content",
    dependencies=[requires(Capability.REPORT_EXPORT)],
    responses=REPORT_CONTENT_RESPONSES,
    tags=["reports"],
    description=DOWNLOAD_SEMANTICS,
)
def get_report_content(
    report_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    observability: ObservabilityDependency,
    format: Annotated[str, Query(pattern="^(markdown|pdf)$")] = "markdown",
) -> StreamingResponse:
    report = repository.get_report(report_id, identity.tenant_id)
    with observability.operation("report.render", identity, format=format) as operation:
        if format == "pdf":
            payload = build_report_pdf(
                report.report,
                list(report.chart_specs) if report.include_charts else None,
            )
            filename = "business-intelligence-report.pdf"
            media_type = "application/pdf"
        else:
            payload = report_to_markdown(report.report).encode("utf-8")
            filename = "business-intelligence-report.md"
            media_type = "text/markdown; charset=utf-8"
        operation.set(size_bytes=len(payload))
    observability.audit(
        "report.downloaded",
        identity,
        resource_id=report.id,
        format=format,
        size_bytes=len(payload),
    )
    return _download(payload, filename, media_type)


@router.post(
    "/messages/{message_id}/exports",
    dependencies=[requires(Capability.REPORT_EXPORT)],
    response_model=ExportResponse,
    status_code=status.HTTP_201_CREATED,
    responses=STANDARD_ERROR_RESPONSES,
    tags=["exports"],
    description=NON_IDEMPOTENT_CREATE_SEMANTICS,
)
def create_export(
    message_id: str,
    command: ExportCreateRequest,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    observability: ObservabilityDependency,
) -> ExportResponse:
    message = repository.get_message(message_id, identity.tenant_id)
    if message.status != "complete":
        raise ResourceConflictError(
            "message_not_complete",
            "Only completed messages can be exported.",
        )
    if message.dataframe is None:
        raise ResourceConflictError(
            "message_has_no_tabular_result",
            "This message does not contain a retained tabular result.",
        )
    dataframe = sanitize_dataframe_for_export(message.dataframe)
    if command.format == "csv":
        payload = dataframe.to_csv(index=False).encode("utf-8")
        filename = "query-result.csv"
        media_type = "text/csv; charset=utf-8"
    else:
        buffer = BytesIO()
        dataframe.to_excel(buffer, index=False)
        payload = buffer.getvalue()
        filename = "query-result.xlsx"
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    record = repository.create_export(
        message=message,
        tenant_id=identity.tenant_id,
        format=command.format,
        filename=filename,
        media_type=media_type,
        payload=payload,
    )
    observability.telemetry.emit(
        "export.create",
        tenant_id=identity.tenant_id,
        format=command.format,
        size_bytes=len(payload),
        row_count=len(dataframe),
    )
    observability.audit(
        "export.created",
        identity,
        resource_id=record.id,
        format=command.format,
        size_bytes=len(payload),
        row_count=len(dataframe),
    )
    return export_response(record)


@router.get(
    "/exports/{export_id}/content",
    dependencies=[requires(Capability.REPORT_EXPORT)],
    responses=EXPORT_CONTENT_RESPONSES,
    tags=["exports"],
    description=DOWNLOAD_SEMANTICS,
)
def get_export_content(
    export_id: str,
    identity: IdentityDependency,
    repository: RepositoryDependency,
    observability: ObservabilityDependency,
) -> StreamingResponse:
    export = repository.get_export(export_id, identity.tenant_id)
    observability.audit(
        "export.downloaded",
        identity,
        resource_id=export.id,
        format=export.format,
        size_bytes=len(export.payload),
    )
    return _download(export.payload, export.filename, export.media_type)


def _answer_attributes(result: OrchestratorResult) -> dict[str, object]:
    """Content-free telemetry attributes for one answered question."""
    diagnostics = result.diagnostics
    attributes: dict[str, object] = {
        "route": result.route,
        "grounding_status": diagnostics.grounding_status,
        "criteria_provenance": diagnostics.criteria_provenance,
        "invalid_citation_count": diagnostics.invalid_citation_count,
        "unverified_quote_count": diagnostics.unverified_quote_count,
        "retrieval_candidates": diagnostics.retrieval_candidates,
        "retrieval_accepted": diagnostics.retrieval_accepted,
        "retrieval_rejected_distance": diagnostics.retrieval_rejected_distance,
        "retrieval_duplicates_skipped": diagnostics.retrieval_duplicates_skipped,
        "retrieval_lexical_candidates": diagnostics.retrieval_lexical_candidates,
        "retrieval_lexical_only_accepted": diagnostics.retrieval_lexical_only_accepted,
        "sql_row_count": diagnostics.sql_row_count,
        "sql_correction_attempted": diagnostics.sql_correction_attempted,
        "structured_output_repairs": diagnostics.structured_output_repairs,
        "structured_output_failures": diagnostics.structured_output_failures,
    }
    if diagnostics.sql_execution_seconds is not None:
        attributes["sql_duration_ms"] = diagnostics.sql_execution_seconds * 1000
    if diagnostics.llm_calls:
        attributes.update(
            llm_calls=diagnostics.llm_calls,
            llm_failures=diagnostics.llm_failures,
            llm_fallbacks=diagnostics.llm_fallbacks,
            llm_duration_ms=diagnostics.llm_duration_ms,
            llm_prompt_tokens=diagnostics.llm_prompt_tokens,
            llm_output_tokens=diagnostics.llm_output_tokens,
            llm_estimated_cost_microusd=(
                round(diagnostics.llm_estimated_cost_usd * 1_000_000)
                if diagnostics.llm_estimated_cost_usd is not None
                else None
            ),
        )
    return {name: value for name, value in attributes.items() if value is not None}


def _safe_pdf_filename(filename: str | None) -> str:
    if filename is None or not filename.strip():
        raise ApiError(422, "missing_filename", "Every PDF must include a filename.")
    safe_name = Path(filename.replace("\\", "/")).name
    if Path(safe_name).suffix.lower() != ".pdf":
        raise ApiError(422, "unsupported_document_type", "Only PDF documents are supported.")
    return safe_name


def _download(payload: bytes, filename: str, media_type: str) -> StreamingResponse:
    return StreamingResponse(
        _iter_download(payload),
        media_type=media_type,
        headers={
            "Accept-Ranges": "none",
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
        },
    )


def _iter_download(payload: bytes, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Yield bounded chunks; a disconnect stops transfer without changing resources."""
    view = memoryview(payload)
    for offset in range(0, len(view), chunk_size):
        yield bytes(view[offset : offset + chunk_size])
