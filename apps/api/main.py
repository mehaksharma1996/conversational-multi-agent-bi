"""FastAPI application composition root."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from fastapi import FastAPI, Request

from apps.api.dependencies import get_repository
from apps.api.errors import ApiError, install_exception_handlers
from apps.api.feature_routes import router as feature_router
from apps.api.models import ErrorResponse, HealthResponse
from apps.api.repository import LocalResourceRepository
from apps.api.routes import router
from config.settings import Settings, get_settings
from packages.analytics import TabularApplicationService
from packages.retrieval import DocumentApplicationService
from src.documents.embedding import SentenceTransformerEmbedder, TextEmbedder
from src.llm.base import LLMClient
from src.llm.gemini_client import build_gemini_client


def create_app(
    settings: Settings | None = None,
    repository: LocalResourceRepository | None = None,
    embedder_factory: Callable[[str], TextEmbedder] | None = None,
    llm_client_factory: Callable[[Settings], LLMClient] | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    application = FastAPI(
        title="Conversational BI API",
        summary="Versioned local API for conversational business intelligence workflows.",
        version="0.5.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    application.state.settings = active_settings
    application.state.repository = repository or LocalResourceRepository(
        storage_root=active_settings.app_data_dir / "api",
        retention_hours=active_settings.session_retention_hours,
    )
    application.state.tabular_service = TabularApplicationService()
    active_embedder_factory = embedder_factory or SentenceTransformerEmbedder
    application.state.document_service = DocumentApplicationService(active_embedder_factory)
    application.state.llm_client_factory = llm_client_factory or build_gemini_client

    @application.middleware("http")
    async def add_request_id(request: Request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

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
        if not active_repository.ready():
            raise ApiError(503, "service_not_ready", "The API is not ready to accept work.")
        return HealthResponse(status="ready")

    application.include_router(router)
    application.include_router(feature_router)
    return application


app = create_app()
