"""FastAPI application composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, nullcontext
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from apps.api.approvals import ApprovalCheckpoints
from apps.api.auth import RequestIdentityProvider, build_identity_provider
from apps.api.auth_routes import router as auth_router
from apps.api.content_store import WorkspaceContentStore
from apps.api.dependencies import get_repository
from apps.api.errors import ApiError, install_exception_handlers
from apps.api.feature_routes import router as feature_router
from apps.api.job_routes import router as job_router
from apps.api.metadata_store import MetadataError, MetadataStore
from apps.api.models import ErrorResponse, HealthResponse
from apps.api.observability import ApiObservability
from apps.api.oidc_login import IdTokenVerifier, OidcLoginService
from apps.api.rate_limit import InMemoryRateLimiter
from apps.api.rehydration import ApiRehydrator
from apps.api.repository import LocalResourceRepository, WorkspaceRecord
from apps.api.routes import router
from apps.api.sessions import InMemorySessionStore
from config.settings import Settings, get_settings
from packages.analytics import TabularApplicationService
from packages.connectors import AuditSink
from packages.governance import JsonlAuditSink, record_configuration
from packages.jobs import InMemoryJobStore, InProcessJobExecutor, JobEvent
from packages.observability import (
    FanOutTelemetrySink,
    LoggingTelemetrySink,
    MetricsRegistry,
    TelemetrySink,
    TraceManager,
    bind_request_id,
    configure_telemetry_logging,
    new_request_id,
)
from packages.retrieval import DocumentApplicationService
from src.documents.embedding import SentenceTransformerEmbedder, TextEmbedder
from src.documents.embedding_cache import EmbeddingVectorCache
from src.llm.base import LLMClient
from src.llm.factory import build_llm_client


def create_app(
    settings: Settings | None = None,
    repository: LocalResourceRepository | None = None,
    embedder_factory: Callable[[str], TextEmbedder] | None = None,
    llm_client_factory: Callable[[Settings], LLMClient] | None = None,
    telemetry_sink: TelemetrySink | None = None,
    audit_sink: AuditSink | None = None,
    identity_provider: RequestIdentityProvider | None = None,
    session_store: InMemorySessionStore | None = None,
    login_service: OidcLoginService | None = None,
    rate_limiter: InMemoryRateLimiter | None = None,
    embedding_cache: EmbeddingVectorCache | None = None,
    job_executor: InProcessJobExecutor | None = None,
    trace_manager: TraceManager | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    active_settings.validate_identity_configuration()
    active_settings.validate_document_index_configuration()

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
    application.state.identity_provider = identity_provider or build_identity_provider(
        active_settings
    )
    application.state.session_store = _build_session_store(active_settings, session_store)
    application.state.login_service = _build_login_service(
        active_settings,
        application.state.identity_provider,
        application.state.session_store,
        login_service,
    )
    storage_root = active_settings.app_data_dir / "api"
    application.state.repository = repository or LocalResourceRepository(
        storage_root=storage_root,
        retention_hours=active_settings.session_retention_hours,
        metadata_store=(
            MetadataStore(storage_root.resolve() / ".metadata" / "metadata.db")
            if active_settings.durable_metadata
            else None
        ),
        content_store=(
            WorkspaceContentStore(storage_root.resolve(), active_settings.sqlite_encryption_key)
            if active_settings.durable_metadata
            else None
        ),
    )
    if telemetry_sink is None:
        configure_telemetry_logging()
    # Metrics and traces are derived from the same allowlisted events as the logs. Tracing remains
    # completely disabled unless an endpoint is configured (packages/observability/tracing.py).
    metrics_registry = MetricsRegistry() if active_settings.metrics_enabled else None
    application.state.metrics_registry = metrics_registry
    active_trace_manager = trace_manager
    if active_trace_manager is None and active_settings.otlp_traces_endpoint is not None:
        active_trace_manager = TraceManager.from_otlp_endpoint(active_settings.otlp_traces_endpoint)
    application.state.trace_manager = active_trace_manager
    base_sink = telemetry_sink or LoggingTelemetrySink()
    telemetry_sinks = [base_sink]
    if metrics_registry is not None:
        telemetry_sinks.append(metrics_registry)
    if active_trace_manager is not None:
        telemetry_sinks.append(active_trace_manager.telemetry_sink)
    observability = ApiObservability.create(
        telemetry_sink=(
            FanOutTelemetrySink(telemetry_sinks) if len(telemetry_sinks) > 1 else base_sink
        ),
        audit_sink=audit_sink
        or JsonlAuditSink(
            active_settings.audit_dir, max_segment_bytes=active_settings.audit_max_segment_bytes
        ),
    )
    application.state.observability = observability
    application.state.approval_checkpoints = ApprovalCheckpoints()
    application.state.rate_limiter = rate_limiter or InMemoryRateLimiter(
        observer=lambda tenant_id, operation, limited, remaining: observability.telemetry.emit(
            "rate_limit.decision",
            tenant_id=tenant_id,
            rate_limit_operation=operation,
            rate_limited=limited,
            rate_limit_remaining=remaining,
        )
    )
    application.state.embedding_cache = embedding_cache or EmbeddingVectorCache(
        active_settings.embedding_cache_max_entries
    )
    application.state.job_executor = job_executor or InProcessJobExecutor(
        InMemoryJobStore(), observer=_job_observer(observability)
    )
    application.state.repository.lifecycle_listener = _workspace_lifecycle_listener(
        observability,
        application.state.approval_checkpoints,
        application.state.rate_limiter,
        application.state.embedding_cache,
        application.state.job_executor,
    )
    application.state.tabular_service = TabularApplicationService()
    active_embedder_factory = embedder_factory or SentenceTransformerEmbedder
    application.state.document_service = DocumentApplicationService(
        active_embedder_factory,
        application.state.embedding_cache,
        cache_observer=lambda tenant_id, hits, misses, entries: observability.telemetry.emit(
            "embedding.cache",
            tenant_id=tenant_id,
            cache_hits=hits,
            cache_misses=misses,
            cache_entries=entries,
        ),
    )
    application.state.repository.rehydrator = ApiRehydrator(
        application.state.tabular_service,
        active_settings,
        application.state.document_service,
    )
    application.state.llm_client_factory = llm_client_factory or build_llm_client

    @application.middleware("http")
    async def correlate_request(request: Request, call_next):
        request_id = new_request_id()
        request.state.request_id = request_id
        started = perf_counter()
        status_code = 500
        observed = not request.url.path.startswith(("/health/", "/metrics"))
        tracing = (
            active_trace_manager.request_span(
                method=request.method,
                request_id=request_id,
                propagation_headers=request.headers,
            )
            if active_trace_manager is not None and observed
            else nullcontext(None)
        )
        with bind_request_id(request_id), tracing as request_span:
            try:
                response = await call_next(request)
                status_code = response.status_code
                response.headers["X-Request-ID"] = request_id
                return response
            finally:
                if observed:
                    route = request.scope.get("route")
                    route_path = getattr(route, "path", "unmatched")
                    error_code = getattr(request.state, "error_code", None)
                    observability.telemetry.emit(
                        "http.request",
                        http_method=request.method,
                        http_route=route_path,
                        status_code=status_code,
                        duration_ms=(perf_counter() - started) * 1000,
                        error_code=error_code,
                    )
                    if request_span is not None:
                        request_span.finish(
                            route=route_path,
                            status_code=status_code,
                            error_code=error_code,
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

    if metrics_registry is not None:

        @application.get("/metrics", include_in_schema=False)
        def metrics() -> PlainTextResponse:
            """Prometheus text exposition. Outside /api and not in the OpenAPI contract."""
            return PlainTextResponse(
                metrics_registry.render(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )

    application.include_router(auth_router)
    application.include_router(router)
    application.include_router(feature_router)
    application.include_router(job_router)
    return application


def _build_session_store(
    settings: Settings, injected: InMemorySessionStore | None
) -> InMemorySessionStore | None:
    """Browser sessions exist only in OIDC mode; local mode never reads a session cookie."""
    if settings.api_auth_mode != "oidc":
        return None
    return injected or InMemorySessionStore(
        max_age_seconds=settings.session_max_age_seconds,
        idle_timeout_seconds=settings.session_idle_timeout_seconds,
    )


def _build_login_service(
    settings: Settings,
    provider: object,
    sessions: InMemorySessionStore | None,
    injected: OidcLoginService | None,
) -> OidcLoginService | None:
    """Enable browser sign-in only when fully configured; never half-enabled."""
    if injected is not None:
        return injected
    if not settings.oidc_login_configured:
        return None
    if sessions is None or not isinstance(provider, IdTokenVerifier):
        raise ValueError("Browser sign-in requires the OIDC identity provider and session store.")
    return OidcLoginService(settings=settings, verifier=provider, sessions=sessions)


def _job_observer(observability: ApiObservability) -> Callable[[JobEvent], None]:
    """Turn job lifecycle events into content-free telemetry (counts, status, duration only)."""

    def observe(event: JobEvent) -> None:
        outcome = {"succeeded": "success", "failed": "failure"}.get(event.status.value)
        with bind_request_id(event.request_id) if event.request_id else nullcontext():
            observability.telemetry.emit(
                "job.transition",
                tenant_id=event.tenant_id,
                job_operation=event.operation,
                job_status=event.status.value,
                job_attempt=event.attempt,
                outcome=outcome,
                error_category=event.error_category,
                duration_ms=event.duration_ms,
            )

    return observe


def _record_configuration(settings: Settings, observability: ApiObservability) -> None:
    """Audit the governed configuration at startup: new or changed values, never secrets."""
    providers = "+".join(settings.llm_providers) or "none"
    values: dict[str, str | bool | int | None] = {
        "llm_providers": providers,
        "gemini_model": settings.gemini_model,
        "gemini_model_fast": settings.gemini_model_fast,
        "anthropic_model": settings.anthropic_model,
        "ollama_model": settings.ollama_model,
        "embedding_model": settings.embedding_model,
        "document_index_backend": settings.document_index_backend,
        "retrieval_hybrid": settings.retrieval_hybrid,
        "local_only_mode": settings.local_only_mode,
        "api_auth_mode": settings.api_auth_mode,
        "durable_metadata": settings.durable_metadata,
        "trace_export_enabled": settings.otlp_traces_endpoint is not None,
        "session_retention_hours": settings.session_retention_hours,
        "sweep_orphaned_workspaces": settings.sweep_orphaned_workspaces,
        "hosted_recipients": "+".join(settings.hosted_recipients()),
    }
    record_configuration(
        observability.recorder,
        observability.audit_sink,
        values,
        llm_provider=providers,
        llm_model=settings.gemini_model,
        authentication_mode=settings.api_auth_mode,
        local_only_mode=settings.local_only_mode,
        durable_metadata=settings.durable_metadata,
    )


def _start(app: FastAPI) -> None:
    """Fail closed on unusable storage, optionally sweep orphans, and announce startup."""
    settings: Settings = app.state.settings
    repository: LocalResourceRepository = app.state.repository
    observability: ApiObservability = app.state.observability
    audit_ready = getattr(observability.audit_sink, "ready", None)
    not_writable = "Workspace or audit storage is not writable; refusing to start."
    try:
        recovery = repository.recover()
    except MetadataError as exc:
        # A newer or altered schema, or a database that cannot be opened even after quarantine.
        raise RuntimeError(f"Workspace metadata cannot be used; refusing to start. {exc}") from exc
    except OSError as exc:
        raise RuntimeError(not_writable) from exc
    if not repository.ready() or (audit_ready is not None and not audit_ready()):
        raise RuntimeError(not_writable)
    swept_count = 0
    failures = 0
    # If the metadata database was quarantined, every workspace directory looks orphaned. Keep them
    # until an operator restores the database or backup; never sweep on top of a recovery failure.
    if settings.sweep_orphaned_workspaces and not recovery.metadata_quarantined:
        result = repository.sweep_orphaned_storage()
        swept_count, failures = len(result.removed), result.failed
        for tenant_id, workspace_id in result.removed:
            observability.recorder.record(
                "workspace.expired",
                tenant_id=tenant_id,
                resource_id=workspace_id,
                reason="orphan_swept",
            )
    _record_configuration(settings, observability)
    observability.telemetry.emit(
        "service.started",
        gemini_configured=settings.hosted_model_configured,
        local_only_mode=settings.local_only_mode,
        sqlite_encrypted=settings.sqlite_encryption_key is not None,
        sweep_enabled=settings.sweep_orphaned_workspaces,
        orphans_swept=swept_count,
        orphan_sweep_failures=failures,
        durable_metadata=recovery.enabled,
        metadata_schema_version=recovery.schema_version,
        metadata_migrations_applied=recovery.migrations_applied,
        metadata_quarantined=recovery.metadata_quarantined,
        workspaces_restored=recovery.workspaces_restored,
        workspaces_expired_on_start=recovery.workspaces_expired,
        uploads_restored=recovery.uploads_restored,
        uploads_dropped=recovery.uploads_dropped,
        datasets_pending=recovery.datasets_pending,
        analyses_pending=recovery.analyses_pending,
        reports_pending=recovery.reports_pending,
        document_collections_pending=recovery.collections_pending,
        conversations_pending=recovery.conversations_pending,
    )


def _stop(app: FastAPI) -> None:
    app.state.job_executor.shutdown()
    app.state.repository.close()
    app.state.observability.telemetry.emit("service.stopped")
    trace_manager: TraceManager | None = app.state.trace_manager
    if trace_manager is not None:
        trace_manager.shutdown()


def _workspace_lifecycle_listener(
    observability: ApiObservability,
    approval_checkpoints: ApprovalCheckpoints,
    rate_limiter: InMemoryRateLimiter,
    embedding_cache: EmbeddingVectorCache,
    job_executor: InProcessJobExecutor,
) -> Callable[[str, WorkspaceRecord, str], None]:
    """Audit workspace lifecycle from the stored (server-side) record's tenant."""

    def listener(event: str, workspace: WorkspaceRecord, reason: str) -> None:
        if event in {"expired", "deleted"}:
            approval_checkpoints.delete_workspace(workspace.id)
            rate_limiter.delete_workspace(workspace.tenant_id, workspace.id)
            embedding_cache.delete_workspace(workspace.tenant_id, workspace.id)
            job_executor.purge_workspace(workspace.tenant_id, workspace.id)
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
