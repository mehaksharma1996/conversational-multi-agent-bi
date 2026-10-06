"""Authentication configuration, token verification, and API-boundary tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from apps.api.auth import OidcBearerIdentityProvider
from apps.api.errors import AuthenticationError, AuthenticationUnavailableError
from apps.api.main import create_app
from config.settings import Settings
from src.utils.identity import derive_tenant_id
from tests.test_utils import isolated_directory_path

ISSUER = "https://identity.example.test"
AUDIENCE = "conversational-bi-api"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"


@pytest.fixture
def signing_key() -> RSAKey:
    return RSAKey.generate_key(auto_kid=True)


def _settings(root: Path, *, auth_mode: str = "oidc") -> Settings:
    return Settings(
        app_data_dir=root,
        sqlite_db_path=root / "sqlite" / "app.db",
        chroma_persist_dir=root / "vectorstore",
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash",
        embedding_model="all-MiniLM-L6-v2",
        api_auth_mode=auth_mode,
        oidc_issuer_url=ISSUER if auth_mode == "oidc" else None,
        oidc_audience=AUDIENCE if auth_mode == "oidc" else None,
        oidc_jwks_url=JWKS_URL if auth_mode == "oidc" else None,
    )


def _token(signing_key: RSAKey, **claim_overrides: Any) -> str:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "iat": int(time()) - 1,
        "exp": int(time()) + 300,
    }
    claims.update(claim_overrides)
    return jwt.encode(
        {"alg": "RS256", "kid": signing_key.kid},
        claims,
        signing_key,
        algorithms=["RS256"],
    )


def _provider(
    signing_key: RSAKey,
    *,
    fetcher: Callable[[str, float], dict[str, Any]] | None = None,
) -> OidcBearerIdentityProvider:
    return OidcBearerIdentityProvider(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS_URL,
        allowed_algorithms=("RS256",),
        clock_skew_seconds=0,
        jwks_cache_seconds=300,
        http_timeout_seconds=2.0,
        jwks_fetcher=fetcher or (lambda _url, _timeout: {"keys": [signing_key.as_dict()]}),
    )


def test_oidc_provider_derives_tenant_only_after_claim_validation(signing_key: RSAKey) -> None:
    identity = _provider(signing_key).authenticate(_token(signing_key))

    assert identity.tenant_id == derive_tenant_id("user-123")
    assert identity.subject == "user-123"
    assert identity.authentication_mode == "oidc"


def test_oidc_provider_caches_jwks(signing_key: RSAKey) -> None:
    calls: list[tuple[str, float]] = []

    def fetcher(url: str, timeout: float) -> dict[str, Any]:
        calls.append((url, timeout))
        return {"keys": [signing_key.as_dict()]}

    provider = _provider(signing_key, fetcher=fetcher)
    token = _token(signing_key)

    provider.authenticate(token)
    provider.authenticate(token)

    assert calls == [(JWKS_URL, 2.0)]


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://attacker.invalid"},
        {"aud": "another-api"},
        {"exp": 1},
        {"sub": ""},
    ],
    ids=["wrong-issuer", "wrong-audience", "expired", "blank-subject"],
)
def test_oidc_provider_rejects_invalid_claims(
    signing_key: RSAKey,
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(AuthenticationError):
        _provider(signing_key).authenticate(_token(signing_key, **overrides))


def test_oidc_provider_rejects_missing_or_badly_signed_token(signing_key: RSAKey) -> None:
    with pytest.raises(AuthenticationError):
        _provider(signing_key).authenticate(None)

    attacker_key = RSAKey.generate_key(auto_kid=True)
    with pytest.raises(AuthenticationError):
        _provider(signing_key).authenticate(_token(attacker_key))

    with pytest.raises(AuthenticationError):
        _provider(signing_key).authenticate("x" * 16_385)


def test_oidc_provider_surfaces_jwks_failure_as_safe_unavailability(signing_key: RSAKey) -> None:
    def unavailable(_url: str, _timeout: float) -> dict[str, Any]:
        raise ValueError("sensitive provider detail")

    with pytest.raises(AuthenticationUnavailableError) as captured:
        _provider(signing_key, fetcher=unavailable).authenticate(_token(signing_key))

    assert str(captured.value) == "The authentication service is temporarily unavailable."
    assert "sensitive" not in str(captured.value)


def test_oidc_provider_rejects_empty_jwks_as_unavailable(signing_key: RSAKey) -> None:
    with pytest.raises(AuthenticationUnavailableError):
        _provider(signing_key, fetcher=lambda _url, _timeout: {"keys": []}).authenticate(
            _token(signing_key)
        )


def test_create_app_validates_identity_settings_even_with_injected_provider(
    signing_key: RSAKey,
) -> None:
    root = isolated_directory_path("api_oidc_invalid_injected")
    invalid_settings = Settings(
        app_data_dir=root,
        sqlite_db_path=root / "sqlite" / "app.db",
        chroma_persist_dir=root / "vectorstore",
        gemini_api_key=None,
        gemini_model="gemini-2.5-flash",
        embedding_model="all-MiniLM-L6-v2",
        api_auth_mode="oidc",
    )

    with pytest.raises(ValueError, match="OIDC mode requires"):
        create_app(settings=invalid_settings, identity_provider=_provider(signing_key))


def test_oidc_mode_requires_bearer_token_at_api_boundary(signing_key: RSAKey) -> None:
    root = isolated_directory_path("api_oidc_required")
    app = create_app(
        settings=_settings(root),
        identity_provider=_provider(signing_key),
    )
    client = TestClient(app)

    assert client.get("/health/live").status_code == 200
    rejected = client.post("/api/v1/workspaces")
    accepted = client.post(
        "/api/v1/workspaces",
        headers={"Authorization": f"Bearer {_token(signing_key)}"},
    )

    assert rejected.status_code == 401
    assert rejected.headers["WWW-Authenticate"] == "Bearer"
    assert rejected.json()["error"]["code"] == "authentication_required"
    assert accepted.status_code == 201
    assert accepted.json()["authentication_mode"] == "oidc"


def test_oidc_provider_outage_is_a_safe_api_503(signing_key: RSAKey) -> None:
    root = isolated_directory_path("api_oidc_unavailable")

    def unavailable(_url: str, _timeout: float) -> dict[str, Any]:
        raise ValueError("provider internals")

    app = create_app(
        settings=_settings(root),
        identity_provider=_provider(signing_key, fetcher=unavailable),
    )
    response = TestClient(app).post(
        "/api/v1/workspaces",
        headers={"Authorization": f"Bearer {_token(signing_key)}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "authentication_unavailable"
    assert "provider internals" not in response.text
