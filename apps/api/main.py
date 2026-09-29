"""FastAPI application composition root."""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request

from apps.api.dependencies import get_repository
from apps.api.errors import ApiError, install_exception_handlers
from apps.api.models import ErrorResponse, HealthResponse
from apps.api.repository import LocalResourceRepository
from apps.api.routes import router
from config.settings import Settings, get_settings
from packages.analytics import TabularApplicationService


def create_app(
    settings: Settings | None = None,
    repository: LocalResourceRepository | None = None,
) -> FastAPI:
    application = FastAPI(
        title="Conversational BI API",
        summary="Versioned local API for conversational business intelligence workflows.",
        version="0.3.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    application.state.settings = settings or get_settings()
    application.state.repository = repository or LocalResourceRepository()
    application.state.tabular_service = TabularApplicationService()

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
    return application


app = create_app()
