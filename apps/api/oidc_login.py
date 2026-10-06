"""OIDC authorization-code + PKCE login for the browser session (ADR 0013).

The service never returns or stores provider tokens: the code is exchanged server-side, the ID
token is verified and discarded, and only the subject, roles, and expiry enter the session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

import httpx

from apps.api.auth import MAX_BEARER_TOKEN_CHARS, MAX_JWKS_BYTES, VerifiedLogin
from apps.api.errors import AuthenticationError, AuthenticationUnavailableError
from apps.api.sessions import BrowserSession, InMemorySessionStore
from config.settings import Settings
from src.utils.identity import derive_tenant_id

LOGIN_BINDING_COOKIE = "__Host-bi_login"
PENDING_LOGIN_TTL_SECONDS = 600
MAX_PENDING_LOGINS = 1_000
MAX_CODE_CHARS = 2_048
MAX_STATE_CHARS = 128
MAX_BINDING_CHARS = 128

TokenExchange = Callable[[str, dict[str, str], float], dict[str, Any]]


@runtime_checkable
class IdTokenVerifier(Protocol):
    def verify_id_token(self, id_token: str, *, client_id: str, nonce: str) -> VerifiedLogin:
        """Return verified identity facts or raise a safe API error."""


class LoginFailedError(Exception):
    """The login attempt is invalid. Carries only an allowlisted reason token, never details.

    ``audited`` is true only once a pending login was matched to the browser that started it, so
    anonymous garbage requests cannot grow the audit log.
    """

    def __init__(self, reason: str = "invalid_request", *, audited: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.audited = audited


@dataclass(frozen=True)
class LoginRedirect:
    authorization_url: str
    binding: str


@dataclass(frozen=True)
class _PendingLogin:
    nonce: str
    code_verifier: str
    binding_digest: str
    expires_at: float


class PendingLoginStore:
    """Single-use, short-lived, bounded record of in-flight logins keyed by ``state``."""

    def __init__(
        self,
        *,
        ttl_seconds: int = PENDING_LOGIN_TTL_SECONDS,
        max_entries: int = MAX_PENDING_LOGINS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._pending: OrderedDict[str, _PendingLogin] = OrderedDict()
        self._lock = Lock()

    def create(self, *, nonce: str, code_verifier: str, binding: str) -> str:
        state = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            for key in [k for k, v in self._pending.items() if now >= v.expires_at]:
                del self._pending[key]
            while len(self._pending) >= self._max_entries:
                self._pending.popitem(last=False)
            self._pending[state] = _PendingLogin(
                nonce=nonce,
                code_verifier=code_verifier,
                binding_digest=_digest(binding),
                expires_at=now + self._ttl_seconds,
            )
        return state

    def consume(self, state: str, binding: str) -> _PendingLogin | None:
        """Remove ``state`` (single use, even on mismatch) and return it only if still valid."""
        if not state or len(state) > MAX_STATE_CHARS:
            return None
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or self._clock() >= pending.expires_at:
            return None
        if not hmac.compare_digest(pending.binding_digest, _digest(binding)):
            return None
        return pending


class OidcLoginService:
    def __init__(
        self,
        *,
        settings: Settings,
        verifier: IdTokenVerifier,
        sessions: InMemorySessionStore,
        token_exchange: TokenExchange | None = None,
        pending: PendingLoginStore | None = None,
    ) -> None:
        assert settings.oidc_authorization_endpoint and settings.oidc_token_endpoint
        assert settings.oidc_client_id and settings.oidc_redirect_uri
        self._authorization_endpoint = settings.oidc_authorization_endpoint
        self._token_endpoint = settings.oidc_token_endpoint
        self._client_id = settings.oidc_client_id
        self._client_secret = settings.oidc_client_secret
        self._redirect_uri = settings.oidc_redirect_uri
        self._scopes = " ".join(settings.oidc_scopes)
        self._timeout = settings.oidc_http_timeout_seconds
        self._verifier = verifier
        self._sessions = sessions
        self._exchange = token_exchange or _post_token_request
        self._pending = pending or PendingLoginStore()

    def begin(self) -> LoginRedirect:
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        binding = secrets.token_urlsafe(32)
        state = self._pending.create(nonce=nonce, code_verifier=code_verifier, binding=binding)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "scope": self._scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in self._authorization_endpoint else "?"
        return LoginRedirect(f"{self._authorization_endpoint}{separator}{query}", binding)

    def complete(
        self, *, code: str | None, state: str | None, binding: str | None
    ) -> tuple[str, BrowserSession]:
        """Finish a login or raise ``LoginFailedError``/``AuthenticationUnavailableError``."""
        pending = self._pending.consume(
            state or "", binding if binding and len(binding) <= MAX_BINDING_CHARS else ""
        )
        if pending is None:
            raise LoginFailedError("invalid_request")
        if not code or len(code) > MAX_CODE_CHARS:
            raise LoginFailedError("no_authorization_code", audited=True)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "client_id": self._client_id,
            "code_verifier": pending.code_verifier,
        }
        if self._client_secret is not None:
            form["client_secret"] = self._client_secret
        try:
            response = self._exchange(self._token_endpoint, form, self._timeout)
        except LoginFailedError as exc:
            raise LoginFailedError("token_exchange_rejected", audited=True) from exc
        id_token = response.get("id_token")
        if not isinstance(id_token, str) or len(id_token) > MAX_BEARER_TOKEN_CHARS:
            raise LoginFailedError("invalid_token_response", audited=True)
        try:
            verified = self._verifier.verify_id_token(
                id_token, client_id=self._client_id, nonce=pending.nonce
            )
        except AuthenticationError as exc:
            raise LoginFailedError("id_token_rejected", audited=True) from exc
        # Provider access/refresh tokens in `response` are intentionally dropped here.
        return self._sessions.create(
            subject=verified.subject,
            tenant_id=derive_tenant_id(verified.subject),
            roles=verified.roles,
            identity_expires_at=verified.expires_at,
        )


def _post_token_request(url: str, form: dict[str, str], timeout_seconds: float) -> dict[str, Any]:
    """POST the code exchange: no redirects, bounded body; 4xx fails the login, else an outage."""
    try:
        with httpx.stream(
            "POST", url, data=form, timeout=timeout_seconds, follow_redirects=False
        ) as response:
            if 400 <= response.status_code < 500:
                raise LoginFailedError
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_JWKS_BYTES:
                    raise AuthenticationUnavailableError()
        document = json.loads(body)
    except (LoginFailedError, AuthenticationUnavailableError):
        raise
    except (httpx.HTTPError, UnicodeError, ValueError) as exc:
        raise AuthenticationUnavailableError() from exc
    if not isinstance(document, dict):
        raise LoginFailedError
    return document


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
