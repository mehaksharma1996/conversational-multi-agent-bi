"""Browser session endpoints (ADR 0013).

These routes authenticate through the session cookie itself rather than a capability: a caller can
only read or end the session it already holds. Sign-in (`login`/`callback`) is a later slice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from apps.api.dependencies import get_browser_session
from apps.api.errors import STANDARD_ERROR_RESPONSES
from apps.api.sessions import SESSION_COOKIE_NAME, BrowserSession, InMemorySessionStore

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])

BrowserSessionDependency = Annotated[BrowserSession, Depends(get_browser_session)]


class SessionResponse(BaseModel):
    """What the SPA may know: its CSRF value, roles for UI gating, and when the session ends."""

    csrf_token: str
    roles: list[str]
    expires_at: datetime


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
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response
