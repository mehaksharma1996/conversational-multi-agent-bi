"""FastAPI application composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, Request

from apps.api.dependencies import get_repository
from apps.api.errors import ApiError, install_exception_handlers
from apps.api.feature_routes import router as feature_router
from apps.api.models import ErrorResponse, HealthResponse
from apps.api.observability import ApiObservability
from apps.api.repository import LocalResourceRepository, WorkspaceRecord
from apps.api.routes import router
from config.settings import Settings, get_settings
from packages.analytics import TabularApplicationService
from packages.connectors import AuditSink
from packages.governance import JsonlAuditSink
from packages.observability import (
    LoggingTelemetrySink,
    TelemetrySink,
    bind_request_id,
    configure_telemetry_logging,
    new_request_id,
)
from packages.retrieval import DocumentApplicationService
from src.documents.embedding import SentenceTransformerEmbedder, TextEmbedder
from src.llm.base import LLMClient
from src.llm.gemini_client import build_gemini_client


def create_app(
    settings: Settings | None = None,
    repository: LocalResourceRepository | None = None,
    embedder_factory: Callable[[str], TextEmbedder] | None = None,
    llm_client_factory: Callable[[Settings], LLMClient] | None = None,
    telemetry_sink: TelemetrySink | None = None,
    audit_sink: AuditSink | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _start(app)
        try:
            yield
        finally:
            _stop(app)

    application = FastAPI(
        title="Conversational BI API",
        summary="Versioned local API for conversational business intelligence workflows.",
        version="0.6.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.state.repository = repository or LocalResourceRepository(
        storage_root=active_settings.app_data_dir / "api",
        retention_hours=active_settings.session_retention_hours,
    )
    if telemetry_sink is None:
        configure_telemetry_logging()
    observability = ApiObservability.create(
        telemetry_sink=telemetry_sink or LoggingTelemetrySink(),
        audit_sink=audit_sink or JsonlAuditSink(active_settings.audit_dir),
    )
    application.state.observability = observability
    application.state.repository.lifecycle_listener = _workspace_lifecycle_listener(observability)
    application.state.tabular_service = TabularApplicationService()
    active_embedder_factory = embedder_factory or SentenceTransformerEmbedder
    application.state.document_service = DocumentApplicationService(active_embedder_factory)
    application.state.llm_client_factory = llm_client_factory or build_gemini_client

    @application.middleware("http")
    async def correlate_request(request: Request, call_next):
        request_id = new_request_id()
        request.state.request_id = request_id
        started = perf_counter()
        status_code = 500
        with bind_request_id(request_id):
            try:
                response = await call_next(request)
                status_code = response.status_code
                response.headers["X-Request-ID"] = request_id
                return response
            finally:
                if not request.url.path.startswith("/health/"):
                    route = request.scope.get("route")
                    observability.telemetry.emit(
                        "http.request",
                        http_method=request.method,
                        http_route=getattr(route, "path", "unmatched"),
                        status_code=status_code,
                        duration_ms=(perf_counter() - started) * 1000,
                        error_code=getattr(request.state, "error_code", None),
                    )

    install_exception_handlers(application)

    @application.get(
        "/health/live",
        response_model=HealthResponse,
        tags=["health"],
    )
    def liveness() -> HealthResponse:
        return HealthResponse(status="alive")

    @application.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse, "description": "Service not ready"}},
        tags=["health"],
    )
    def readiness(request: Request) -> HealthResponse:
        active_repository = get_repository(request)
        audit_ready = getattr(request.app.state.observability.audit_sink, "ready", None)
        if not active_repository.ready() or (audit_ready is not None and not audit_ready()):
            raise ApiError(503, "service_not_ready", "The API is not ready to accept work.")
        return HealthResponse(status="ready")

    application.include_router(router)
    application.include_router(feature_router)
    return application


def _start(app: FastAPI) -> None:
    """Fail closed on unusable storage, optionally sweep orphans, and announce startup."""
    settings: Settings = app.state.settings
    repository: LocalResourceRepository = app.state.repository
    observability: ApiObservability = app.state.observability
    audit_ready = getattr(observability.audit_sink, "ready", None)
    if not repository.ready() or (audit_ready is not None and not audit_ready()):
        raise RuntimeError("Workspace or audit storage is not writable; refusing to start.")
    swept_count = 0
    failures = 0
    if settings.sweep_orphaned_workspaces:
        result = repository.sweep_orphaned_storage()
        swept_count, failures = len(result.removed), result.failed
        for tenant_id, workspace_id in result.removed:
            observability.recorder.record(
                "workspace.expired",
                tenant_id=tenant_id,
                resource_id=workspace_id,
                reason="orphan_swept",
            )
    observability.telemetry.emit(
        "service.started",
        gemini_configured=settings.gemini_configured,
        local_only_mode=settings.local_only_mode,
        sqlite_encrypted=settings.sqlite_encryption_key is not None,
        sweep_enabled=settings.sweep_orphaned_workspaces,
        orphans_swept=swept_count,
        orphan_sweep_failures=failures,
    )


def _stop(app: FastAPI) -> None:
    app.state.repository.close()
    app.state.observability.telemetry.emit("service.stopped")


def _workspace_lifecycle_listener(
    observability: ApiObservability,
) -> Callable[[str, WorkspaceRecord, str], None]:
    """Audit workspace lifecycle from the stored (server-side) record's tenant."""

    def listener(event: str, workspace: WorkspaceRecord, reason: str) -> None:
        observability.telemetry.emit(
            "workspace.lifecycle",
            tenant_id=workspace.tenant_id,
            workspace_event=event,
            authentication_mode=workspace.authentication_mode,
        )
        attributes: dict[str, object] = {"reason": reason}
        if event == "created":
            attributes.update(
                authentication_mode=workspace.authentication_mode,
                gemini_configured=workspace.gemini_configured,
                local_only_mode=workspace.local_only_mode,
            )
        observability.recorder.record(
            f"workspace.{event}",
            tenant_id=workspace.tenant_id,
            resource_id=workspace.id,
            **attributes,
        )

    return listener


app = create_app()
