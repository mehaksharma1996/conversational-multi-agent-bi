"""Server-side browser sessions (ADR 0013).

The browser holds only an opaque random identifier in an HttpOnly cookie. Provider tokens are never
stored. Only a SHA-256 digest of the identifier is kept, so a memory dump of the store cannot be
replayed as a cookie.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import Lock

SESSION_COOKIE_NAME = "__Host-bi_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
# Attributes shared by every cookie this API sets or clears, so set/delete always match.
COOKIE_ATTRIBUTES = {"path": "/", "secure": True, "httponly": True, "samesite": "lax"}
MAX_SESSIONS = 10_000
_MAX_IDENTIFIER_CHARS = 128


@dataclass(frozen=True)
class BrowserSession:
    """Verified identity facts captured once at sign-in; never request-supplied."""

    subject: str
    tenant_id: str
    roles: frozenset[str]
    csrf_token: str
    created_at: float
    expires_at: float
    last_seen_at: float


class InMemorySessionStore:
    """Bounded, thread-safe session store; a restart ends every session (ADR 0010)."""

    def __init__(
        self,
        *,
        max_age_seconds: int,
        idle_timeout_seconds: int,
        clock: Callable[[], float] = time.time,
        max_sessions: int = MAX_SESSIONS,
    ) -> None:
        self._max_age_seconds = max_age_seconds
        self._idle_timeout_seconds = idle_timeout_seconds
        self._clock = clock
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, BrowserSession] = OrderedDict()
        self._lock = Lock()

    def create(
        self,
        *,
        subject: str,
        tenant_id: str,
        roles: frozenset[str],
        identity_expires_at: float | None = None,
    ) -> tuple[str, BrowserSession]:
        """Create a session and return ``(session_id, session)``.

        The session ends at the earliest of the absolute limit and the verified identity expiry.
        """
        now = self._clock()
        expires_at = now + self._max_age_seconds
        if identity_expires_at is not None:
            expires_at = min(expires_at, identity_expires_at)
        session = BrowserSession(
            subject=subject,
            tenant_id=tenant_id,
            roles=roles,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=expires_at,
            last_seen_at=now,
        )
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            self._evict_expired(now)
            while len(self._sessions) >= self._max_sessions:
                self._sessions.popitem(last=False)
            self._sessions[_digest(session_id)] = session
        return session_id, session

    def get(self, session_id: str) -> BrowserSession | None:
        """Return the live session and refresh its idle clock, or ``None``."""
        if not session_id or len(session_id) > _MAX_IDENTIFIER_CHARS:
            return None
        key = _digest(session_id)
        now = self._clock()
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if (
                now >= session.expires_at
                or now - session.last_seen_at >= self._idle_timeout_seconds
            ):
                del self._sessions[key]
                return None
            refreshed = replace(session, last_seen_at=now)
            self._sessions[key] = refreshed
            self._sessions.move_to_end(key)
            return refreshed

    def revoke(self, session_id: str) -> None:
        if not session_id or len(session_id) > _MAX_IDENTIFIER_CHARS:
            return
        with self._lock:
            self._sessions.pop(_digest(session_id), None)

    def _evict_expired(self, now: float) -> None:
        expired = [
            key
            for key, session in self._sessions.items()
            if now >= session.expires_at or now - session.last_seen_at >= self._idle_timeout_seconds
        ]
        for key in expired:
            del self._sessions[key]


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()
