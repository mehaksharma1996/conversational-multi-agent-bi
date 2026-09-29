"""FastAPI dependencies for trusted identity and application components."""

from __future__ import annotations

from fastapi import Request

from apps.api.observability import ApiObservability
from apps.api.repository import LocalResourceRepository
from config.settings import Settings
from packages.analytics import TabularApplicationService
from packages.connectors import IdentityContext
from packages.retrieval import DocumentApplicationService
from src.llm.base import LLMClient
from src.utils.identity import LOCAL_DEV_TENANT_ID


def get_identity() -> IdentityContext:
    """Return the explicit local-development identity.

    Browser-provided tenant headers are deliberately ignored. A future OIDC
    adapter will replace this dependency with verified server-side claims.
    """
    return IdentityContext(
        tenant_id=LOCAL_DEV_TENANT_ID,
        authentication_mode="local",
    )


def get_repository(request: Request) -> LocalResourceRepository:
    return request.app.state.repository


def get_observability(request: Request) -> ApiObservability:
    return request.app.state.observability


def get_tabular_service(request: Request) -> TabularApplicationService:
    return request.app.state.tabular_service


def get_document_service(request: Request) -> DocumentApplicationService:
    return request.app.state.document_service


def get_llm_client(request: Request) -> LLMClient:
    return request.app.state.llm_client_factory(request.app.state.settings)


def get_api_settings(request: Request) -> Settings:
    return request.app.state.settings
