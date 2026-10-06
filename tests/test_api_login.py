"""OIDC authorization-code + PKCE browser login (ADR 0013), offline and deterministic."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from time import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from apps.api import oidc_login
from apps.api.auth import OidcBearerIdentityProvider
from apps.api.errors import AuthenticationUnavailableError
from apps.api.main import create_app
from apps.api.oidc_login import LoginFailedError, OidcLoginService, PendingLoginStore
from apps.api.sessions import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    BrowserSession,
    InMemorySessionStore,
)
from config.settings import Settings
from tests.test_api_auth import AUDIENCE, ISSUER, _provider, _settings
from tests.test_utils import isolated_directory_path

ORIGIN = "https://bi.example.test"
CLIENT_ID = "bi-web"
AUTHORIZE = "https://identity.example.test/authorize"
TOKEN = "https://identity.example.test/token"
FAILED = "/?auth_error=login_failed"
UNAVAILABLE = "/?auth_error=provider_unavailable"


def _login_settings(name: str, **overrides: Any) -> Settings:
    base = replace(
        _settings(isolated_directory_path(name)),
        web_origin=ORIGIN,
        oidc_authorization_endpoint=AUTHORIZE,
        oidc_token_endpoint=TOKEN,
        oidc_client_id=CLIENT_ID,
        oidc_redirect_uri=f"{ORIGIN}/api/v1/auth/callback",
        oidc_scopes=("openid", "roles"),
    )
    return replace(base, **overrides)


class Exchange:
    """Fake token endpoint that signs an ID token for whatever nonce the login used."""

    def __init__(self, key: RSAKey) -> None:
        self.key = key
        self.calls: list[dict[str, str]] = []
        self.nonce = ""
        self.claims: dict[str, Any] = {}
        self.response_override: dict[str, Any] | None = None
        self.raises: Exception | None = None

    def __call__(self, url: str, form: dict[str, str], timeout: float) -> dict[str, Any]:
        assert url == TOKEN
        self.calls.append(form)
        if self.raises is not None:
            raise self.raises
        if self.response_override is not None:
            return self.response_override
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "user-123",
            "nonce": self.nonce,
            "iat": int(time()) - 1,
            "exp": int(time()) + 300,
            "roles": ["analyst"],
        }
        claims.update(self.claims)
        id_token = jwt.encode(
            {"alg": "RS256", "kid": self.key.kid}, claims, self.key, algorithms=["RS256"]
        )
        return {
            "id_token": id_token,
            "access_token": "provider-access-token",
            "refresh_token": "provider-refresh-token",
            "token_type": "Bearer",
        }


class Harness:
    def __init__(self, name: str, **setting_overrides: Any) -> None:
        self.key = RSAKey.generate_key(auto_kid=True)
        self.settings = _login_settings(name, **setting_overrides)
        self.provider: OidcBearerIdentityProvider = _provider(self.key)
        self.store = InMemorySessionStore(max_age_seconds=3600, idle_timeout_seconds=600)
        self.clock = [1_000_000.0]
        self.pending = PendingLoginStore(clock=lambda: self.clock[0])
        self.exchange = Exchange(self.key)
        service = OidcLoginService(
            settings=self.settings,
            verifier=self.provider,
            sessions=self.store,
            token_exchange=self.exchange,
            pending=self.pending,
        )
        app = create_app(
            settings=self.settings,
            identity_provider=self.provider,
            session_store=self.store,
            login_service=service,
        )
        self.client = TestClient(app, base_url="https://testserver", follow_redirects=False)

    def begin(self) -> dict[str, list[str]]:
        response = self.client.get("/api/v1/auth/login")
        assert response.status_code == 302
        query = parse_qs(urlparse(response.headers["location"]).query)
        self.exchange.nonce = query["nonce"][0]
        return query

    def callback(self, state: str, code: str = "auth-code", **extra: str) -> httpx.Response:
        return self.client.get(
            "/api/v1/auth/callback", params={"code": code, "state": state, **extra}
        )

    def sign_in(self) -> httpx.Response:
        return self.callback(self.begin()["state"][0])


@pytest.fixture
def harness() -> Harness:
    return Harness("login_flow")


# --- happy path --------------------------------------------------------------------------------


def test_login_redirects_with_pkce_state_nonce_and_a_bound_cookie(harness: Harness) -> None:
    response = harness.client.get("/api/v1/auth/login")
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)

    assert response.status_code == 302
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTHORIZE
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"{ORIGIN}/api/v1/auth/callback"]
    assert query["scope"] == ["openid roles"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) >= 32 and len(query["nonce"][0]) >= 32
    assert "client_secret" not in response.headers["location"]
    assert response.headers["Cache-Control"] == "no-store"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-bi_login=")
    assert {"HttpOnly", "Secure", "SameSite=lax", "Max-Age=600", "Path=/"} <= set(
        part.strip() for part in cookie.split(";")
    )


def test_successful_callback_creates_a_session_and_discards_provider_tokens(
    harness: Harness,
) -> None:
    query = harness.begin()
    challenge = query["code_challenge"][0]

    response = harness.callback(query["state"][0])

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE_NAME}="))
    assert {"HttpOnly", "Secure", "SameSite=lax", "Path=/"} <= {
        part.strip() for part in session_cookie.split(";")
    }
    assert any(c.startswith("__Host-bi_login=") and "Max-Age=0" in c for c in cookies)
    form = harness.exchange.calls[0]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "auth-code" and form["client_id"] == CLIENT_ID
    assert form["redirect_uri"] == f"{ORIGIN}/api/v1/auth/callback"
    assert "client_secret" not in form
    verifier_digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(verifier_digest).rstrip(b"=").decode() == challenge

    session = harness.client.get("/api/v1/auth/session")
    assert session.status_code == 200
    assert session.json()["roles"] == ["analyst"]
    blob = response.text + str(response.headers) + session.text
    for secret in ("provider-access-token", "provider-refresh-token", "auth-code"):
        assert secret not in blob
    assert not {f.name for f in dataclasses.fields(BrowserSession)} & {
        "access_token",
        "refresh_token",
        "id_token",
    }


def test_confidential_client_secret_is_sent_only_to_the_token_endpoint() -> None:
    harness = Harness("login_secret", oidc_client_secret="s3cret-value")

    login = harness.client.get("/api/v1/auth/login")
    harness.exchange.nonce = parse_qs(urlparse(login.headers["location"]).query)["nonce"][0]
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    response = harness.callback(state)

    assert harness.exchange.calls[0]["client_secret"] == "s3cret-value"
    assert "s3cret-value" not in login.headers["location"] + str(response.headers)
    assert "s3cret-value" not in repr(harness.settings)


def test_session_roles_and_csrf_apply_after_login(harness: Harness) -> None:
    harness.exchange.claims = {"roles": ["viewer"]}
    harness.sign_in()
    csrf = harness.client.get("/api/v1/auth/session").json()["csrf_token"]
    headers = {"Origin": ORIGIN, CSRF_HEADER_NAME: csrf}

    assert harness.client.post("/api/v1/workspaces").json()["error"]["code"] == "csrf_failed"
    denied = harness.client.post("/api/v1/workspaces", headers=headers)
    assert denied.json()["error"]["code"] == "permission_denied"


def test_session_lifetime_never_exceeds_the_id_token(harness: Harness) -> None:
    harness.exchange.claims = {"exp": int(time()) + 45}

    response = harness.sign_in()

    cookie = next(c for c in response.headers.get_list("set-cookie") if SESSION_COOKIE_NAME in c)
    max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
    assert 1 <= max_age <= 46


def test_default_wiring_builds_the_login_service_from_settings() -> None:
    key = RSAKey.generate_key(auto_kid=True)
    app = create_app(settings=_login_settings("login_default"), identity_provider=_provider(key))

    response = TestClient(app, base_url="https://testserver", follow_redirects=False).get(
        "/api/v1/auth/login"
    )

    assert response.status_code == 302
    assert response.headers["location"].startswith(AUTHORIZE + "?")


# --- failure paths -----------------------------------------------------------------------------


def _assert_failed(response: httpx.Response, target: str = FAILED) -> None:
    assert response.status_code == 302
    assert response.headers["location"] == target
    assert not any(SESSION_COOKIE_NAME in c for c in response.headers.get_list("set-cookie"))
    assert response.headers["Cache-Control"] == "no-store"


def test_state_is_single_use(harness: Harness) -> None:
    state = harness.begin()["state"][0]
    binding = harness.client.cookies["__Host-bi_login"]
    assert harness.callback(state).headers["location"] == "/"
    harness.client.cookies.clear()

    # Replay with the *valid* binding cookie still present: only single-use state can stop it.
    replay = harness.client.get(
        "/api/v1/auth/callback",
        params={"code": "auth-code", "state": state},
        headers={"Cookie": f"__Host-bi_login={binding}"},
    )

    _assert_failed(replay)
    assert len(harness.exchange.calls) == 1


@pytest.mark.parametrize("state", ["", "unknown-state", "x" * 500])
def test_unknown_or_oversized_state_fails(harness: Harness, state: str) -> None:
    harness.begin()

    _assert_failed(harness.callback(state))


def test_missing_or_foreign_binding_cookie_fails_login_csrf(harness: Harness) -> None:
    state = harness.begin()["state"][0]
    harness.client.cookies.clear()
    _assert_failed(harness.callback(state))

    state = harness.begin()["state"][0]
    harness.client.cookies.set("__Host-bi_login", "attacker-binding", domain="testserver.local")
    harness.client.cookies.clear()
    harness.client.headers["Cookie"] = "__Host-bi_login=attacker-binding"
    _assert_failed(harness.callback(state))
    assert harness.exchange.calls == []  # never reached the token endpoint


def test_pending_login_expires(harness: Harness) -> None:
    state = harness.begin()["state"][0]
    harness.clock[0] += 601

    _assert_failed(harness.callback(state))


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "different-nonce"},
        {"aud": "another-client"},
        {"aud": AUDIENCE},
        {"iss": "https://attacker.invalid"},
        {"exp": 1},
        {"sub": ""},
    ],
    ids=["nonce", "audience", "access-token-audience", "issuer", "expired", "blank-subject"],
)
def test_invalid_id_token_claims_fail(harness: Harness, claims: dict[str, Any]) -> None:
    state = harness.begin()["state"][0]
    harness.exchange.claims = claims

    _assert_failed(harness.callback(state))
    assert len(harness.store._sessions) == 0


def test_missing_nonce_claim_and_foreign_signature_fail(harness: Harness) -> None:
    state = harness.begin()["state"][0]
    harness.exchange.key = RSAKey.generate_key(auto_kid=True)  # attacker-signed
    _assert_failed(harness.callback(state))

    state = harness.begin()["state"][0]
    harness.exchange.key = harness.key
    harness.exchange.nonce = ""  # token carries an empty/unrelated nonce
    _assert_failed(harness.callback(state))


@pytest.mark.parametrize(
    "response",
    [{}, {"access_token": "only-an-access-token"}, {"id_token": 123}, {"id_token": "x" * 20_000}],
    ids=["empty", "access-token-only", "non-string", "oversized"],
)
def test_token_response_without_a_valid_id_token_fails(
    harness: Harness, response: dict[str, Any]
) -> None:
    state = harness.begin()["state"][0]
    harness.exchange.response_override = response

    _assert_failed(harness.callback(state))


def test_provider_rejection_and_outage_are_distinct_safe_outcomes(harness: Harness) -> None:
    state = harness.begin()["state"][0]
    harness.exchange.raises = LoginFailedError()
    _assert_failed(harness.callback(state))

    state = harness.begin()["state"][0]
    harness.exchange.raises = AuthenticationUnavailableError()
    _assert_failed(harness.callback(state), UNAVAILABLE)


def test_provider_error_parameter_is_never_reflected_and_still_consumes_state(
    harness: Harness,
) -> None:
    state = harness.begin()["state"][0]

    response = harness.client.get(
        "/api/v1/auth/callback",
        params={
            "error": "access_denied",
            "error_description": "<script>x</script>",
            "state": state,
        },
    )

    _assert_failed(response)
    assert "access_denied" not in str(response.headers) + response.text
    assert harness.exchange.calls == []
    _assert_failed(harness.callback(state))


@pytest.mark.parametrize("code", ["", "c" * 2_049])
def test_missing_or_oversized_code_fails(harness: Harness, code: str) -> None:
    state = harness.begin()["state"][0]

    _assert_failed(harness.callback(state, code=code))
    assert harness.exchange.calls == []


def test_redirect_target_is_never_client_controlled(harness: Harness) -> None:
    state = harness.begin()["state"][0]

    response = harness.callback(
        state, next="https://evil.example", return_to="//evil.example", redirect_uri="https://evil"
    )

    assert response.headers["location"] == "/"
    assert harness.exchange.calls[0]["redirect_uri"] == f"{ORIGIN}/api/v1/auth/callback"


def test_login_routes_are_404_when_sign_in_is_not_configured() -> None:
    key = RSAKey.generate_key(auto_kid=True)
    oidc_only = create_app(
        settings=_settings(isolated_directory_path("login_off")), identity_provider=_provider(key)
    )
    local = create_app(
        settings=_settings(isolated_directory_path("login_local"), auth_mode="local")
    )

    for app in (oidc_only, local):
        client = TestClient(app, base_url="https://testserver", follow_redirects=False)
        for path in ("/api/v1/auth/login", "/api/v1/auth/callback"):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "login_not_configured"


def test_login_requires_a_verifier_and_session_store() -> None:
    class NotAVerifier:
        def authenticate(self, token: str | None) -> Any:  # pragma: no cover
            raise AssertionError

    with pytest.raises(ValueError, match="Browser sign-in requires"):
        create_app(settings=_login_settings("login_bad_wiring"), identity_provider=NotAVerifier())


# --- token endpoint transport ------------------------------------------------------------------


@contextmanager
def _fake_stream(response: httpx.Response) -> Iterator[httpx.Response]:
    yield response


def _call_with(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def stream(method: str, url: str, **kwargs: Any) -> Any:
        captured.update(method=method, url=url, **kwargs)
        return _fake_stream(response)

    monkeypatch.setattr(oidc_login.httpx, "stream", stream)
    result = oidc_login._post_token_request(TOKEN, {"code": "c"}, 2.0)
    assert captured["method"] == "POST" and captured["follow_redirects"] is False
    assert captured["timeout"] == 2.0
    return result


def _response(status: int, content: bytes = b"{}") -> httpx.Response:
    return httpx.Response(status, content=content, request=httpx.Request("POST", TOKEN))


def test_token_exchange_returns_bounded_json(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _call_with(monkeypatch, _response(200, b'{"id_token": "t"}')) == {"id_token": "t"}


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_response(400), LoginFailedError),
        (_response(401), LoginFailedError),
        (_response(200, b"[]"), LoginFailedError),
        (_response(500), AuthenticationUnavailableError),
        (_response(302), AuthenticationUnavailableError),
        (_response(200, b"not json"), AuthenticationUnavailableError),
        (_response(200, b"{" + b" " * 1_000_001 + b"}"), AuthenticationUnavailableError),
    ],
    ids=["400", "401", "array", "500", "redirect", "not-json", "oversized"],
)
def test_token_exchange_failure_modes(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response, expected: type[Exception]
) -> None:
    with pytest.raises(expected):
        _call_with(monkeypatch, response)


def test_token_exchange_network_errors_are_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    def stream(*_a: Any, **_k: Any) -> Any:
        raise httpx.ConnectTimeout("provider detail")

    monkeypatch.setattr(oidc_login.httpx, "stream", stream)

    with pytest.raises(AuthenticationUnavailableError) as captured:
        oidc_login._post_token_request(TOKEN, {}, 1.0)
    assert "provider detail" not in str(captured.value)


# --- settings ----------------------------------------------------------------------------------

CALLBACK = f"{ORIGIN}/api/v1/auth/callback"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"oidc_token_endpoint": None}, "Browser login requires"),
        ({"web_origin": None}, "Browser login requires"),
        ({"oidc_client_id": None, "oidc_client_secret": "x"}, "Browser login requires"),
        ({"oidc_authorization_endpoint": "http://idp.test/auth"}, "HTTPS"),
        ({"oidc_token_endpoint": "https://u:p@idp.test/token"}, "HTTPS"),
        ({"oidc_redirect_uri": "https://other.example/api/v1/auth/callback"}, "REDIRECT_URI"),
        ({"oidc_redirect_uri": f"{ORIGIN}/other"}, "REDIRECT_URI"),
        ({"oidc_redirect_uri": f"{CALLBACK}?x=1"}, "REDIRECT_URI"),
        ({"oidc_scopes": ("profile",)}, "OIDC_SCOPES"),
        ({"oidc_scopes": ("openid", "bad scope;")}, "OIDC_SCOPES"),
        ({"oidc_client_secret": "  "}, "invalid"),
    ],
)
def test_partial_or_unsafe_login_configuration_fails_closed(
    overrides: dict[str, Any], message: str
) -> None:
    settings = replace(_login_settings("login_settings"), **overrides)

    with pytest.raises(ValueError, match=message):
        settings.validate_identity_configuration()


def test_secret_without_login_and_login_in_local_mode_are_rejected() -> None:
    oidc = _settings(isolated_directory_path("login_secret_only"))
    with pytest.raises(ValueError, match="CLIENT_SECRET"):
        replace(oidc, web_origin=ORIGIN, oidc_client_secret="x").validate_identity_configuration()

    local = _settings(isolated_directory_path("login_local_cfg"), auth_mode="local")
    overrides: list[dict[str, Any]] = [{"oidc_client_id": CLIENT_ID}, {"oidc_client_secret": "x"}]
    for override in overrides:
        with pytest.raises(ValueError, match="silent local fallback"):
            replace(local, **override).validate_identity_configuration()

    _login_settings("login_ok").validate_identity_configuration()
