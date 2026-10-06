"""Browser session store, cookie identity, CSRF, logout, and no-CORS behavior (ADR 0013)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from apps.api.main import create_app
from apps.api.sessions import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    BrowserSession,
    InMemorySessionStore,
)
from config.settings import Settings
from src.utils.identity import derive_tenant_id
from tests.test_api_auth import _provider, _settings
from tests.test_utils import isolated_directory_path

ORIGIN = "https://bi.example.test"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> InMemorySessionStore:
    return InMemorySessionStore(max_age_seconds=3600, idle_timeout_seconds=600, clock=clock)


def _client(
    store: InMemorySessionStore, name: str, *, web_origin: str | None = ORIGIN
) -> TestClient:
    settings = replace(_settings(isolated_directory_path(name)), web_origin=web_origin)
    app: FastAPI = create_app(
        settings=settings,
        identity_provider=_provider(RSAKey.generate_key(auto_kid=True)),
        session_store=store,
    )
    return TestClient(app, base_url="https://testserver")


def _sign_in(
    store: InMemorySessionStore, subject: str = "user-1", roles: tuple[str, ...] = ("analyst",)
) -> tuple[dict[str, str], BrowserSession]:
    session_id, session = store.create(
        subject=subject,
        tenant_id=derive_tenant_id(subject),
        roles=frozenset(roles),
    )
    return {"Cookie": f"{SESSION_COOKIE_NAME}={session_id}"}, session


def _unsafe(headers: dict[str, str], session: BrowserSession, **extra: str) -> dict[str, str]:
    return {**headers, "Origin": ORIGIN, CSRF_HEADER_NAME: session.csrf_token, **extra}


# --- store -------------------------------------------------------------------------------------


def test_store_keeps_only_a_digest_of_the_identifier(store: InMemorySessionStore) -> None:
    session_id, _ = store.create(subject="s", tenant_id="t", roles=frozenset())

    assert session_id not in store._sessions
    assert store.get(session_id) is not None


def test_store_expires_sessions_by_idle_and_absolute_limits(
    store: InMemorySessionStore, clock: Clock
) -> None:
    idle_id, _ = store.create(subject="a", tenant_id="t", roles=frozenset())
    clock.now += 599
    assert store.get(idle_id) is not None  # activity refreshes the idle clock
    clock.now += 599
    assert store.get(idle_id) is not None
    clock.now += 601
    assert store.get(idle_id) is None

    absolute_id, _ = store.create(subject="b", tenant_id="t", roles=frozenset())
    for _ in range(7):
        clock.now += 500
        assert store.get(absolute_id) is not None or clock.now >= 1_000_000 + 3600
    clock.now += 500
    assert store.get(absolute_id) is None


def test_store_caps_lifetime_at_verified_identity_expiry(
    store: InMemorySessionStore, clock: Clock
) -> None:
    session_id, session = store.create(
        subject="s", tenant_id="t", roles=frozenset(), identity_expires_at=clock.now + 30
    )

    assert session.expires_at == clock.now + 30
    clock.now += 31
    assert store.get(session_id) is None


@pytest.mark.parametrize("identifier", ["", "unknown", "x" * 129])
def test_store_rejects_unknown_or_oversized_identifiers(
    store: InMemorySessionStore, identifier: str
) -> None:
    assert store.get(identifier) is None
    store.revoke(identifier)


def test_store_is_bounded_and_revocable(clock: Clock) -> None:
    small = InMemorySessionStore(
        max_age_seconds=3600, idle_timeout_seconds=600, clock=clock, max_sessions=2
    )
    first, _ = small.create(subject="1", tenant_id="t", roles=frozenset())
    second, _ = small.create(subject="2", tenant_id="t", roles=frozenset())
    third, _ = small.create(subject="3", tenant_id="t", roles=frozenset())

    assert small.get(first) is None
    small.revoke(second)
    assert small.get(second) is None
    assert small.get(third) is not None


# --- cookie identity ---------------------------------------------------------------------------


def test_cookie_session_authenticates_and_scopes_tenant(store: InMemorySessionStore) -> None:
    client = _client(store, "sess_identity")
    owner, owner_session = _sign_in(store, "owner")
    intruder, _ = _sign_in(store, "intruder")

    created = client.post("/api/v1/workspaces", headers=_unsafe(owner, owner_session))
    workspace_id = created.json()["id"]

    assert created.status_code == 201
    assert client.get(f"/api/v1/workspaces/{workspace_id}", headers=owner).status_code == 200
    assert client.get(f"/api/v1/workspaces/{workspace_id}", headers=intruder).status_code == 404


def test_session_roles_drive_capabilities(store: InMemorySessionStore) -> None:
    client = _client(store, "sess_roles")
    viewer, viewer_session = _sign_in(store, "viewer-user", ("viewer",))

    assert client.get("/api/v1/workspaces/missing", headers=viewer).status_code == 404
    denied = client.post("/api/v1/workspaces", headers=_unsafe(viewer, viewer_session))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"


@pytest.mark.parametrize("cookie", ["", "bogus", "x" * 500])
def test_missing_unknown_or_oversized_cookie_is_a_safe_401(
    store: InMemorySessionStore, cookie: str
) -> None:
    client = _client(store, "sess_unknown")

    response = client.get(
        "/api/v1/workspaces/w", headers={"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert cookie not in response.text or cookie == ""


def test_expired_session_is_rejected(store: InMemorySessionStore, clock: Clock) -> None:
    client = _client(store, "sess_expired")
    headers, _ = _sign_in(store)

    clock.now += 3601

    assert client.get("/api/v1/workspaces/w", headers=headers).status_code == 401


def test_bearer_credential_takes_precedence_over_cookie(store: InMemorySessionStore) -> None:
    client = _client(store, "sess_precedence")
    headers, _ = _sign_in(store, roles=("workspace_admin",))

    response = client.get(
        "/api/v1/workspaces/w", headers={**headers, "Authorization": "Bearer not-a-jwt"}
    )

    assert response.status_code == 401  # the invalid bearer is judged, not silently replaced


# --- CSRF --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h, s: {k: v for k, v in h.items() if k != CSRF_HEADER_NAME},
        lambda h, s: {**h, CSRF_HEADER_NAME: "wrong-token"},
        lambda h, s: {**h, CSRF_HEADER_NAME: ""},
        lambda h, s: {k: v for k, v in h.items() if k != "Origin"},
        lambda h, s: {**h, "Origin": "https://attacker.example.test"},
        lambda h, s: {**h, "Origin": "null"},
        lambda h, s: {**h, "Origin": ORIGIN + "."},
    ],
    ids=["no-token", "wrong-token", "empty-token", "no-origin", "wrong-origin", "null", "suffix"],
)
def test_state_changing_cookie_requests_require_csrf_token_and_origin(
    store: InMemorySessionStore, mutate
) -> None:
    client = _client(store, "sess_csrf")
    headers, session = _sign_in(store)

    response = client.post("/api/v1/workspaces", headers=mutate(_unsafe(headers, session), session))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert session.csrf_token not in response.text


def test_csrf_token_from_another_session_is_rejected(store: InMemorySessionStore) -> None:
    client = _client(store, "sess_csrf_cross")
    first, _ = _sign_in(store, "first")
    _, second_session = _sign_in(store, "second")

    response = client.post("/api/v1/workspaces", headers=_unsafe(first, second_session))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_cookie_writes_fail_closed_without_a_configured_web_origin(
    store: InMemorySessionStore,
) -> None:
    client = _client(store, "sess_no_origin", web_origin=None)
    headers, session = _sign_in(store)

    assert client.post("/api/v1/workspaces", headers=_unsafe(headers, session)).status_code == 403
    assert client.get("/api/v1/workspaces/missing", headers=headers).status_code == 404


# --- session and logout endpoints ----------------------------------------------------------------


def test_session_endpoint_exposes_only_csrf_roles_and_expiry(
    store: InMemorySessionStore,
) -> None:
    client = _client(store, "sess_endpoint")
    headers, session = _sign_in(store, "private-subject", ("analyst", "viewer"))

    response = client.get("/api/v1/auth/session", headers=headers)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"csrf_token", "roles", "expires_at"}
    assert body["csrf_token"] == session.csrf_token
    assert body["roles"] == ["analyst", "viewer"]
    assert "private-subject" not in response.text
    assert client.get("/api/v1/auth/session").status_code == 401


def test_logout_requires_csrf_revokes_server_side_and_expires_cookie(
    store: InMemorySessionStore,
) -> None:
    client = _client(store, "sess_logout")
    headers, session = _sign_in(store)

    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 403
    assert client.get("/api/v1/auth/session", headers=headers).status_code == 200

    response = client.post("/api/v1/auth/logout", headers=_unsafe(headers, session))

    assert response.status_code == 204
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()
    assert "Secure" in set_cookie and "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie
    assert client.get("/api/v1/auth/session", headers=headers).status_code == 401
    assert client.get("/api/v1/workspaces/w", headers=headers).status_code == 401


def test_local_mode_ignores_session_cookies_and_has_no_session_endpoints_access() -> None:
    root = isolated_directory_path("sess_local")
    client = TestClient(
        create_app(settings=_settings(root, auth_mode="local")), base_url="https://testserver"
    )

    assert client.get("/api/v1/auth/session").status_code == 401
    assert (
        client.get(
            "/api/v1/workspaces/missing", headers={"Cookie": f"{SESSION_COOKIE_NAME}=anything"}
        ).status_code
        == 404
    )


# --- CORS --------------------------------------------------------------------------------------


def test_api_sends_no_cors_headers_and_rejects_cross_origin_preflight(
    store: InMemorySessionStore,
) -> None:
    client = _client(store, "sess_cors")
    headers, _ = _sign_in(store)
    attacker = {"Origin": "https://attacker.example.test"}

    preflight = client.options(
        "/api/v1/workspaces",
        headers={**attacker, "Access-Control-Request-Method": "POST"},
    )
    actual = client.get("/api/v1/workspaces/missing", headers={**headers, **attacker})

    for response in (preflight, actual):
        assert not any(name.lower().startswith("access-control-") for name in response.headers)
    assert preflight.status_code in {400, 404, 405}


# --- settings ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "http://bi.example.test",
        "https://bi.example.test/app",
        "https://bi.example.test?x=1",
        "https://user:pw@bi.example.test",
        "ftp://bi.example.test",
        "bi.example.test",
    ],
)
def test_web_origin_must_be_a_bare_https_origin(origin: str) -> None:
    settings = replace(_settings(isolated_directory_path("sess_settings")), web_origin=origin)

    with pytest.raises(ValueError, match="WEB_ORIGIN"):
        settings.validate_identity_configuration()


@pytest.mark.parametrize("origin", ["https://bi.example.test", "http://127.0.0.1:8080"])
def test_valid_web_origins_are_accepted(origin: str) -> None:
    settings = replace(_settings(isolated_directory_path("sess_settings_ok")), web_origin=origin)

    settings.validate_identity_configuration()


def test_web_origin_cannot_be_set_in_local_mode_and_timeouts_are_bounded() -> None:
    base: Settings = _settings(isolated_directory_path("sess_settings_local"), auth_mode="local")
    with pytest.raises(ValueError, match="silent local fallback"):
        replace(base, web_origin=ORIGIN).validate_identity_configuration()

    oidc = replace(_settings(isolated_directory_path("sess_settings_oidc")), web_origin=ORIGIN)
    with pytest.raises(ValueError, match="IDLE_TIMEOUT"):
        replace(oidc, session_idle_timeout_seconds=10).validate_identity_configuration()
    with pytest.raises(ValueError, match="MAX_AGE"):
        replace(
            oidc, session_max_age_seconds=100, session_idle_timeout_seconds=600
        ).validate_identity_configuration()
