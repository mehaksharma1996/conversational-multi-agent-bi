"""Trusted API identity providers.

Only verified server-side claims become an :class:`IdentityContext`. Request paths, headers, and
token claims never supply a tenant identifier directly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any, Protocol

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from apps.api.errors import AuthenticationError, AuthenticationUnavailableError
from config.settings import Settings
from packages.connectors import IdentityContext
from src.utils.identity import LOCAL_DEV_TENANT_ID, derive_tenant_id

MAX_JWKS_BYTES = 1_000_000
MAX_BEARER_TOKEN_CHARS = 16_384
MAX_ROLE_CLAIM_ITEMS = 32
MAX_ROLE_CHARS = 64

JwksFetcher = Callable[[str, float], dict[str, Any]]


@dataclass(frozen=True)
class VerifiedLogin:
    """Identity facts from a verified ID token; the token itself is never retained."""

    subject: str
    roles: frozenset[str]
    expires_at: float


class RequestIdentityProvider(Protocol):
    """Resolve a trusted identity from an optional bearer credential."""

    def authenticate(self, bearer_token: str | None) -> IdentityContext:
        """Return a verified identity or raise a safe API error."""


class LocalDevelopmentIdentityProvider:
    """Explicit single-tenant identity used only by local-development mode."""

    def authenticate(self, bearer_token: str | None) -> IdentityContext:
        del bearer_token
        return IdentityContext(
            tenant_id=LOCAL_DEV_TENANT_ID,
            authentication_mode="local",
        )


class OidcBearerIdentityProvider:
    """Verify OIDC bearer JWTs against configured, cached provider keys."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        allowed_algorithms: tuple[str, ...],
        clock_skew_seconds: int,
        jwks_cache_seconds: int,
        http_timeout_seconds: float,
        roles_claim: str = "roles",
        jwks_fetcher: JwksFetcher | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._allowed_algorithms = allowed_algorithms
        self._clock_skew_seconds = clock_skew_seconds
        self._jwks_cache_seconds = jwks_cache_seconds
        self._http_timeout_seconds = http_timeout_seconds
        self._roles_claim = roles_claim
        self._jwks_fetcher = jwks_fetcher or _fetch_jwks
        self._monotonic_clock = monotonic_clock
        self._keys: KeySet | None = None
        self._keys_expire_at = 0.0
        self._keys_lock = Lock()

    def authenticate(self, bearer_token: str | None) -> IdentityContext:
        if (
            bearer_token is None
            or not bearer_token.strip()
            or len(bearer_token) > MAX_BEARER_TOKEN_CHARS
        ):
            raise AuthenticationError()
        claims = self._verified_claims(bearer_token, audience=self._audience)
        subject = claims["sub"]
        return IdentityContext(
            tenant_id=derive_tenant_id(subject),
            subject=subject,
            authentication_mode="oidc",
            roles=_roles_from_claims(claims, self._roles_claim),
        )

    def verify_id_token(self, id_token: str, *, client_id: str, nonce: str) -> VerifiedLogin:
        """Verify a code-flow ID token (signature, issuer, client audience, nonce, expiry)."""
        if not id_token.strip() or len(id_token) > MAX_BEARER_TOKEN_CHARS:
            raise AuthenticationError()
        claims = self._verified_claims(id_token, audience=client_id, nonce=nonce)
        expires_at = claims["exp"]
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            raise AuthenticationError()
        return VerifiedLogin(
            subject=claims["sub"],
            roles=_roles_from_claims(claims, self._roles_claim),
            expires_at=float(expires_at),
        )

    def _verified_claims(
        self, token_text: str, *, audience: str, nonce: str | None = None
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if nonce is not None:
            extra["nonce"] = {"essential": True, "value": nonce}
        try:
            token = jwt.decode(
                token_text,
                self._get_keys(),
                algorithms=self._allowed_algorithms,
            )
            JWTClaimsRegistry(
                leeway=self._clock_skew_seconds,
                iss={"essential": True, "value": self._issuer},
                aud={"essential": True, "value": audience},
                sub={"essential": True},
                exp={"essential": True},
                **extra,
            ).validate(token.claims)
            subject = token.claims.get("sub")
            if not isinstance(subject, str) or not subject.strip():
                raise AuthenticationError()
        except AuthenticationUnavailableError:
            raise
        except (JoseError, TypeError, ValueError, UnicodeError) as exc:
            raise AuthenticationError() from exc
        return dict(token.claims)

    def _get_keys(self) -> KeySet:
        now = self._monotonic_clock()
        with self._keys_lock:
            if self._keys is not None and now < self._keys_expire_at:
                return self._keys
            try:
                document = self._jwks_fetcher(self._jwks_url, self._http_timeout_seconds)
                raw_keys = document.get("keys")
                if not isinstance(raw_keys, list) or not raw_keys:
                    raise AuthenticationUnavailableError()
                keys = KeySet.import_key_set(document)
            except AuthenticationUnavailableError:
                raise
            except (JoseError, TypeError, ValueError, httpx.HTTPError) as exc:
                raise AuthenticationUnavailableError() from exc
            self._keys = keys
            self._keys_expire_at = now + self._jwks_cache_seconds
            return keys


def build_identity_provider(
    settings: Settings,
    *,
    jwks_fetcher: JwksFetcher | None = None,
) -> RequestIdentityProvider:
    """Build the configured provider only after fail-closed validation."""
    settings.validate_identity_configuration()
    if settings.api_auth_mode == "local":
        return LocalDevelopmentIdentityProvider()
    assert settings.oidc_issuer_url is not None
    assert settings.oidc_audience is not None
    assert settings.oidc_jwks_url is not None
    return OidcBearerIdentityProvider(
        issuer=settings.oidc_issuer_url,
        audience=settings.oidc_audience,
        jwks_url=settings.oidc_jwks_url,
        allowed_algorithms=settings.oidc_allowed_algorithms,
        clock_skew_seconds=settings.oidc_clock_skew_seconds,
        jwks_cache_seconds=settings.oidc_jwks_cache_seconds,
        http_timeout_seconds=settings.oidc_http_timeout_seconds,
        roles_claim=settings.oidc_roles_claim,
        jwks_fetcher=jwks_fetcher,
    )


def _roles_from_claims(claims: dict[str, Any], claim_name: str) -> frozenset[str]:
    """Read a bounded list of role strings; any other shape yields no roles (fail closed)."""
    value = claims.get(claim_name)
    if not isinstance(value, list) or len(value) > MAX_ROLE_CLAIM_ITEMS:
        return frozenset()
    return frozenset(
        role for role in value if isinstance(role, str) and 0 < len(role) <= MAX_ROLE_CHARS
    )


def _fetch_jwks(url: str, timeout_seconds: float) -> dict[str, Any]:
    try:
        with httpx.stream(
            "GET",
            url,
            timeout=timeout_seconds,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > MAX_JWKS_BYTES:
                raise AuthenticationUnavailableError()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_JWKS_BYTES:
                    raise AuthenticationUnavailableError()
        document = json.loads(body)
    except AuthenticationUnavailableError:
        raise
    except (httpx.HTTPError, UnicodeError, ValueError) as exc:
        raise AuthenticationUnavailableError() from exc
    if not isinstance(document, dict):
        raise AuthenticationUnavailableError()
    return document
