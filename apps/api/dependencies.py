"""FastAPI dependencies for trusted identity and application components."""

from __future__ import annotations

from typing import Annotated

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from apps.api.approvals import ApprovalCheckpoints
from apps.api.auth import RequestIdentityProvider
from apps.api.observability import ApiObservability
from apps.api.repository import LocalResourceRepository
from config.settings import Settings
from packages.analytics import TabularApplicationService
from packages.connectors import IdentityContext
from packages.retrieval import DocumentApplicationService
from src.llm.base import LLMClient

bearer_scheme = HTTPBearer(auto_error=False)


def get_identity(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ],
) -> IdentityContext:
    """Resolve identity through the application-owned trusted provider."""
    provider: RequestIdentityProvider = request.app.state.identity_provider
    token = credentials.credentials if credentials is not None else None
    return provider.authenticate(token)


def get_repository(request: Request) -> LocalResourceRepository:
    return request.app.state.repository


def get_observability(request: Request) -> ApiObservability:
    return request.app.state.observability


def get_approval_checkpoints(request: Request) -> ApprovalCheckpoints:
    return request.app.state.approval_checkpoints


def get_tabular_service(request: Request) -> TabularApplicationService:
    return request.app.state.tabular_service


def get_document_service(request: Request) -> DocumentApplicationService:
    return request.app.state.document_service


def get_llm_client(request: Request) -> LLMClient:
    return request.app.state.llm_client_factory(request.app.state.settings)


def get_api_settings(request: Request) -> Settings:
    return request.app.state.settings
