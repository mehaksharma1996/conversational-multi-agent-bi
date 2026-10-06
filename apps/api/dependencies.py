"""FastAPI dependencies for trusted identity and application components."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

from apps.api.approvals import ApprovalCheckpoints
from apps.api.auth import RequestIdentityProvider
from apps.api.errors import AuthenticationError, CsrfError
from apps.api.observability import ApiObservability
from apps.api.rate_limit import InMemoryRateLimiter
from apps.api.repository import LocalResourceRepository
from apps.api.sessions import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    BrowserSession,
    InMemorySessionStore,
)
from config.settings import Settings
from packages.analytics import TabularApplicationService
from packages.connectors import IdentityContext
from packages.retrieval import DocumentApplicationService
from src.llm.base import LLMClient
from src.llm.observability import LLMCallRecord, LLMPricing, ObservedLLMClient

bearer_scheme = HTTPBearer(auto_error=False)
session_cookie_scheme = APIKeyCookie(name=SESSION_COOKIE_NAME, auto_error=False)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_identity(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ],
    session_cookie: Annotated[str | None, Security(session_cookie_scheme)] = None,
) -> IdentityContext:
    """Resolve identity through the application-owned trusted provider.

    A Bearer credential (API clients) takes precedence. Otherwise a valid browser session cookie
    supplies the identity captured at sign-in; cookie-authenticated state-changing requests
    must pass CSRF verification. Tenant, role, and capability are never read from request input.
    """
    provider: RequestIdentityProvider = request.app.state.identity_provider
    if credentials is not None:
        return provider.authenticate(credentials.credentials)
    store: InMemorySessionStore | None = request.app.state.session_store
    if store is None or session_cookie is None:
        return provider.authenticate(None)
    session = _live_session(store, session_cookie)
    _enforce_csrf(request, session)
    return IdentityContext(
        tenant_id=session.tenant_id,
        subject=session.subject,
        authentication_mode="oidc",
        roles=session.roles,
    )


def get_browser_session(
    request: Request,
    session_cookie: Annotated[str | None, Security(session_cookie_scheme)] = None,
) -> BrowserSession:
    """Resolve only the cookie session (for ``/auth`` routes); applies CSRF to unsafe methods."""
    store: InMemorySessionStore | None = request.app.state.session_store
    if store is None or session_cookie is None:
        raise AuthenticationError()
    session = _live_session(store, session_cookie)
    _enforce_csrf(request, session)
    return session


def _live_session(store: InMemorySessionStore, cookie: str) -> BrowserSession:
    session = store.get(cookie)
    if session is None:
        raise AuthenticationError()
    return session


def _enforce_csrf(request: Request, session: BrowserSession) -> None:
    if request.method in SAFE_METHODS:
        return
    settings: Settings = request.app.state.settings
    allowed_origin = settings.web_origin.rstrip("/") if settings.web_origin else None
    origin = request.headers.get("Origin")
    supplied = request.headers.get(CSRF_HEADER_NAME, "")
    origin_ok = allowed_origin is not None and origin == allowed_origin
    token_ok = hmac.compare_digest(supplied.encode(), session.csrf_token.encode())
    if not (origin_ok and token_ok):
        observability: ApiObservability = request.app.state.observability
        observability.audit_tenant(
            "auth.csrf_rejected",
            session.tenant_id,
            authentication_mode="oidc",
            reason="origin_mismatch" if not origin_ok else "token_mismatch",
        )
        raise CsrfError()


def get_repository(request: Request) -> LocalResourceRepository:
    return request.app.state.repository


def get_observability(request: Request) -> ApiObservability:
    return request.app.state.observability


def get_approval_checkpoints(request: Request) -> ApprovalCheckpoints:
    return request.app.state.approval_checkpoints


def get_rate_limiter(request: Request) -> InMemoryRateLimiter:
    return request.app.state.rate_limiter


def get_tabular_service(request: Request) -> TabularApplicationService:
    return request.app.state.tabular_service


def get_document_service(request: Request) -> DocumentApplicationService:
    return request.app.state.document_service


def get_llm_client(
    request: Request,
    identity: Annotated[IdentityContext, Depends(get_identity)],
) -> LLMClient:
    """Wrap the configured provider so every model call emits one content-free ``llm.call``."""
    settings: Settings = request.app.state.settings
    inner = request.app.state.llm_client_factory(settings)
    observability: ApiObservability = request.app.state.observability
    pricing = (
        LLMPricing(
            settings.llm_input_cost_per_million_usd,
            settings.llm_output_cost_per_million_usd,
        )
        if settings.llm_input_cost_per_million_usd is not None
        and settings.llm_output_cost_per_million_usd is not None
        else None
    )

    def observe(record: LLMCallRecord) -> None:
        observability.telemetry.emit(
            "llm.call",
            tenant_id=identity.tenant_id,
            llm_purpose=record.purpose,
            llm_provider=record.provider,
            llm_model=record.model,
            duration_ms=record.duration_ms,
            outcome=record.outcome,
            error_category=record.error_category,
            llm_retries=record.retries,
            llm_fallbacks=record.fallbacks,
            llm_prompt_tokens=record.prompt_tokens,
            llm_output_tokens=record.output_tokens,
            llm_estimated_cost_microusd=(
                round(record.estimated_cost_usd * 1_000_000)
                if record.estimated_cost_usd is not None
                else None
            ),
        )

    return ObservedLLMClient(inner, observe, pricing)


def get_api_settings(request: Request) -> Settings:
    return request.app.state.settings
