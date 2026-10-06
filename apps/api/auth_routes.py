"""Browser sign-in and session endpoints (ADR 0013).

`login` and `callback` run the OIDC authorization-code + PKCE flow and are public by necessity;
`session` and `logout` authenticate with the session cookie itself, so a caller can only read or end
the session it already holds. Redirect targets are fixed constants: nothing from the request or the
provider is ever reflected into a URL.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from apps.api.dependencies import get_browser_session
from apps.api.errors import (
    STANDARD_ERROR_RESPONSES,
    ApiError,
    AuthenticationUnavailableError,
)
from apps.api.oidc_login import (
    LOGIN_BINDING_COOKIE,
    PENDING_LOGIN_TTL_SECONDS,
    LoginFailedError,
    OidcLoginService,
)
from apps.api.sessions import (
    COOKIE_ATTRIBUTES,
    SESSION_COOKIE_NAME,
    BrowserSession,
    InMemorySessionStore,
)

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])

BrowserSessionDependency = Annotated[BrowserSession, Depends(get_browser_session)]

SIGNED_IN_PATH = "/"
FAILED_PATH = "/?auth_error=login_failed"
UNAVAILABLE_PATH = "/?auth_error=provider_unavailable"
REDIRECT_RESPONSES: dict[int | str, dict[str, str]] = {
    302: {"description": "Redirect to the identity provider or back to the application"},
    404: {"description": "Browser sign-in is not configured"},
}


def _login_service(request: Request) -> OidcLoginService:
    service: OidcLoginService | None = request.app.state.login_service
    if service is None:
        raise ApiError(404, "login_not_configured", "Browser sign-in is not configured.")
    return service


def _redirect(path: str) -> RedirectResponse:
    response = RedirectResponse(path, status_code=status.HTTP_302_FOUND)
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(LOGIN_BINDING_COOKIE, **COOKIE_ATTRIBUTES)
    return response


class SessionResponse(BaseModel):
    """What the SPA may know: its CSRF value, roles for UI gating, and when the session ends."""

    csrf_token: str
    roles: list[str]
    expires_at: datetime


@router.get(
    "/login",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses=REDIRECT_RESPONSES,
    summary="Begin browser sign-in",
)
def login(request: Request) -> RedirectResponse:
    redirect = _login_service(request).begin()
    response = RedirectResponse(redirect.authorization_url, status_code=status.HTTP_302_FOUND)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        LOGIN_BINDING_COOKIE,
        redirect.binding,
        max_age=PENDING_LOGIN_TTL_SECONDS,
        **COOKIE_ATTRIBUTES,
    )
    return response


@router.get(
    "/callback",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses=REDIRECT_RESPONSES,
    summary="Complete browser sign-in",
)
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    service = _login_service(request)
    binding = request.cookies.get(LOGIN_BINDING_COOKIE)
    try:
        # A provider-reported `error` still consumes `state`, but never reaches a URL or a log.
        session_id, session = service.complete(
            code=None if error else code, state=state, binding=binding
        )
    except LoginFailedError:
        return _redirect(FAILED_PATH)
    except AuthenticationUnavailableError:
        return _redirect(UNAVAILABLE_PATH)
    response = _redirect(SIGNED_IN_PATH)
    remaining = max(1, int(session.expires_at - time.time()))
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        max_age=remaining,
        **COOKIE_ATTRIBUTES,
    )
    return response


@router.get(
    "/session",
    response_model=SessionResponse,
    responses=STANDARD_ERROR_RESPONSES,
    summary="Describe the current browser session",
)
def read_session(session: BrowserSessionDependency, response: Response) -> SessionResponse:
    response.headers["Cache-Control"] = "no-store"
    return SessionResponse(
        csrf_token=session.csrf_token,
        roles=sorted(session.roles),
        expires_at=datetime.fromtimestamp(session.expires_at, tz=UTC),
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=STANDARD_ERROR_RESPONSES,
    summary="End the current browser session",
)
def logout(request: Request, _session: BrowserSessionDependency) -> Response:
    store: InMemorySessionStore = request.app.state.session_store
    store.revoke(request.cookies[SESSION_COOKIE_NAME])
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE_NAME, **COOKIE_ATTRIBUTES)
    response.headers["Cache-Control"] = "no-store"
    return response
