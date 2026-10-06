"""Authentication and authorization audit events: coverage, trust, privacy, fail-safe."""

from __future__ import annotations

from typing import Any

import pytest

from apps.api.errors import AuthenticationUnavailableError
from apps.api.oidc_login import LoginFailedError
from apps.api.sessions import CSRF_HEADER_NAME
from packages.connectors import AuditEvent
from packages.governance import ANONYMOUS_TENANT_ID, AUDIT_ACTIONS, InMemoryAuditSink
from src.utils.identity import derive_tenant_id
from tests.test_api_login import ORIGIN, Harness

USER_TENANT = derive_tenant_id("user-123")
SECRET_FRAGMENTS = (
    "user-123",
    "auth-code",
    "provider-access-token",
    "provider-refresh-token",
    "Bearer",
    "eyJ",  # any JWT
)


@pytest.fixture
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def harness(audit: InMemoryAuditSink) -> Harness:
    return Harness("auth_audit", audit_sink=audit)


def _names(audit: InMemoryAuditSink) -> list[str]:
    return [event.name for event in audit.events]


def _assert_no_secrets(events: list[AuditEvent], *extra: str) -> None:
    blob = repr(events).lower()
    for fragment in (*SECRET_FRAGMENTS, *extra):
        assert fragment.lower() not in blob, fragment


def test_new_actions_are_registered() -> None:
    assert {
        "auth.login_succeeded",
        "auth.login_failed",
        "auth.logout",
        "auth.csrf_rejected",
        "authz.denied",
    } <= AUDIT_ACTIONS


def test_successful_login_is_audited_under_the_verified_tenant(
    harness: Harness, audit: InMemoryAuditSink
) -> None:
    query = harness.begin()
    harness.callback(query["state"][0])

    [event] = audit.events
    assert event.name == "auth.login_succeeded"
    assert event.tenant_id == USER_TENANT
    assert event.attributes == {"authentication_mode": "oidc"}
    _assert_no_secrets(audit.events, query["state"][0], query["nonce"][0])


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda h: setattr(h.exchange, "raises", LoginFailedError()), "token_exchange_rejected"),
        (lambda h: setattr(h.exchange, "claims", {"aud": "other"}), "id_token_rejected"),
        (lambda h: setattr(h.exchange, "response_override", {}), "invalid_token_response"),
        (
            lambda h: setattr(h.exchange, "raises", AuthenticationUnavailableError()),
            "provider_unavailable",
        ),
    ],
    ids=["exchange-rejected", "bad-id-token", "no-id-token", "provider-outage"],
)
def test_failed_login_after_a_valid_start_is_audited_anonymously_with_a_reason(
    harness: Harness, audit: InMemoryAuditSink, setup: Any, reason: str
) -> None:
    state = harness.begin()["state"][0]
    setup(harness)

    harness.callback(state)

    [event] = audit.events
    assert event.name == "auth.login_failed"
    assert event.tenant_id == ANONYMOUS_TENANT_ID
    assert event.attributes == {"authentication_mode": "oidc", "reason": reason}
    _assert_no_secrets(audit.events, state)


def test_provider_error_parameter_is_audited_without_its_text(
    harness: Harness, audit: InMemoryAuditSink
) -> None:
    state = harness.begin()["state"][0]

    harness.client.get(
        "/api/v1/auth/callback",
        params={"error": "access_denied", "error_description": "secret detail", "state": state},
    )

    [event] = audit.events
    assert event.attributes["reason"] == "no_authorization_code"
    _assert_no_secrets(audit.events, "access_denied", "secret detail")


def test_anonymous_garbage_cannot_grow_the_audit_log(
    harness: Harness, audit: InMemoryAuditSink
) -> None:
    harness.begin()
    for state in ("", "unknown", "x" * 500):
        harness.callback(state)
    unbound_state = harness.begin()["state"][0]
    harness.client.cookies.clear()
    harness.callback(unbound_state)  # valid state, but the browser lost its binding cookie
    harness.client.get("/api/v1/auth/session")
    harness.client.get("/api/v1/workspaces/w")
    harness.client.get("/api/v1/workspaces/w", headers={"Authorization": "Bearer garbage"})

    assert audit.events == []


def test_logout_is_audited_for_the_session_tenant(
    harness: Harness, audit: InMemoryAuditSink
) -> None:
    harness.sign_in()
    csrf = harness.client.get("/api/v1/auth/session").json()["csrf_token"]

    harness.client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN, CSRF_HEADER_NAME: csrf})

    assert _names(audit) == ["auth.login_succeeded", "auth.logout"]
    assert audit.events[1].tenant_id == USER_TENANT
    assert audit.events[1].attributes == {"authentication_mode": "oidc"}
    _assert_no_secrets(audit.events, csrf)


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({"Origin": ORIGIN}, "token_mismatch"),
        ({"Origin": ORIGIN, CSRF_HEADER_NAME: "wrong"}, "token_mismatch"),
        ({"Origin": "https://attacker.example.test"}, "origin_mismatch"),
        ({}, "origin_mismatch"),
    ],
)
def test_csrf_rejection_is_audited_for_the_authenticated_session(
    harness: Harness, audit: InMemoryAuditSink, headers: dict[str, str], reason: str
) -> None:
    harness.sign_in()
    csrf = harness.client.get("/api/v1/auth/session").json()["csrf_token"]
    before = len(audit.events)

    response = harness.client.post("/api/v1/workspaces", headers=headers)

    assert response.status_code == 403
    [event] = audit.events[before:]
    assert event.name == "auth.csrf_rejected"
    assert event.tenant_id == USER_TENANT
    assert event.attributes == {"authentication_mode": "oidc", "reason": reason}
    _assert_no_secrets(audit.events[before:], csrf)


def test_authorization_denial_is_audited_with_the_server_owned_capability(
    harness: Harness, audit: InMemoryAuditSink
) -> None:
    harness.exchange.claims = {"roles": ["viewer"]}
    harness.sign_in()
    csrf = harness.client.get("/api/v1/auth/session").json()["csrf_token"]
    before = len(audit.events)

    denied = harness.client.post(
        "/api/v1/workspaces", headers={"Origin": ORIGIN, CSRF_HEADER_NAME: csrf}
    )
    allowed = harness.client.get("/api/v1/workspaces/missing")

    assert denied.status_code == 403 and allowed.status_code == 404
    [event] = audit.events[before:]  # the permitted read emitted nothing
    assert event.name == "authz.denied"
    assert event.tenant_id == USER_TENANT
    assert event.attributes == {"capability": "data:write", "authentication_mode": "oidc"}
    assert "dropped_attributes" not in event.attributes
    _assert_no_secrets(audit.events[before:], csrf)


def test_request_supplied_tenant_never_selects_the_audit_tenant(
    harness: Harness, audit: InMemoryAuditSink
) -> None:
    harness.exchange.claims = {"roles": ["viewer"]}
    harness.sign_in()
    csrf = harness.client.get("/api/v1/auth/session").json()["csrf_token"]

    harness.client.post(
        "/api/v1/workspaces?tenant_id=" + "f" * 32,
        headers={"Origin": ORIGIN, CSRF_HEADER_NAME: csrf, "X-Tenant-ID": "f" * 32},
        json={"tenant_id": "f" * 32},
    )

    assert {event.tenant_id for event in audit.events} == {USER_TENANT}


def test_audit_write_failures_never_break_sign_in_or_denial() -> None:
    class BrokenSink:
        def record(self, event: AuditEvent) -> None:
            raise OSError("disk full")

    harness = Harness("auth_audit_broken", audit_sink=BrokenSink())
    harness.exchange.claims = {"roles": ["viewer"]}

    login = harness.sign_in()
    csrf = harness.client.get("/api/v1/auth/session").json()["csrf_token"]
    denied = harness.client.post(
        "/api/v1/workspaces", headers={"Origin": ORIGIN, CSRF_HEADER_NAME: csrf}
    )

    assert login.headers["location"] == "/"
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"


def test_default_jsonl_sink_keeps_a_verifiable_chain_for_auth_events() -> None:
    harness = Harness("auth_audit_jsonl")
    harness.sign_in()
    csrf = harness.client.get("/api/v1/auth/session").json()["csrf_token"]
    harness.client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN, CSRF_HEADER_NAME: csrf})
    state = harness.begin()["state"][0]
    harness.exchange.claims = {"aud": "other"}
    harness.callback(state)

    sink = harness.client.app.state.observability.audit_sink

    assert sink.verify(USER_TENANT)
    assert sink.verify(ANONYMOUS_TENANT_ID)
