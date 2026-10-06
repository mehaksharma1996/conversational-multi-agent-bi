"""Capability authorization: role mapping, route coverage, and negative API paths."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from apps.api.authorization import (
    ROLE_CAPABILITIES,
    Capability,
    CapabilityRequirement,
    capabilities_for,
)
from apps.api.feature_routes import router as feature_router
from apps.api.main import create_app
from apps.api.routes import router as resource_router
from packages.connectors import IdentityContext
from tests.test_api_auth import _provider, _settings, _token
from tests.test_utils import isolated_directory_path


@pytest.fixture
def signing_key() -> RSAKey:
    return RSAKey.generate_key(auto_kid=True)


def _app(signing_key: RSAKey, name: str) -> FastAPI:
    return create_app(
        settings=_settings(isolated_directory_path(name)),
        identity_provider=_provider(signing_key),
    )


def _headers(signing_key: RSAKey, subject: str = "user-123", **claims: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(signing_key, sub=subject, **claims)}"}


def _api_routes(app: FastAPI) -> list[APIRoute]:
    """Every /api/v1 route, cross-checked against the app's OpenAPI operations."""
    routes = [
        route
        for router in (resource_router, feature_router)
        for route in router.routes
        if isinstance(route, APIRoute)
    ]
    declared = {(method, route.path) for route in routes for method in route.methods}
    published = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1")
        for method in operations
    }
    assert declared == published
    assert len(routes) >= 22
    return routes


def _concrete_path(route: APIRoute) -> str:
    return route.path.replace("{", "").replace("}", "")


def test_role_mapping_is_closed_and_least_privilege() -> None:
    assert set(ROLE_CAPABILITIES) == {"viewer", "analyst", "workspace_admin"}
    assert ROLE_CAPABILITIES["viewer"] == {Capability.WORKSPACE_READ}
    assert Capability.WORKSPACE_ADMIN not in ROLE_CAPABILITIES["analyst"]
    assert ROLE_CAPABILITIES["workspace_admin"] == set(Capability)


@pytest.mark.parametrize("roles", [frozenset(), frozenset({"root", "admin", "VIEWER", ""})])
def test_unknown_or_missing_roles_grant_nothing(roles: frozenset[str]) -> None:
    identity = IdentityContext(tenant_id="t", authentication_mode="oidc", roles=roles)

    assert capabilities_for(identity) == frozenset()


def test_only_explicit_local_mode_receives_implicit_capabilities() -> None:
    assert capabilities_for(IdentityContext(tenant_id="t", authentication_mode="test")) == set()
    assert capabilities_for(IdentityContext(tenant_id="t")) == set(Capability)


def test_every_protected_route_declares_a_capability(signing_key: RSAKey) -> None:
    routes = _api_routes(_app(signing_key, "authz_route_coverage"))

    undeclared = [
        f"{sorted(route.methods)} {route.path}"
        for route in routes
        if not any(
            isinstance(dependency.dependency, CapabilityRequirement)
            for dependency in route.dependencies
        )
    ]
    assert undeclared == []


def test_anonymous_requests_get_401_not_403_on_every_route(signing_key: RSAKey) -> None:
    app = _app(signing_key, "authz_anonymous")
    client = TestClient(app)

    for route in _api_routes(app):
        for method in route.methods - {"HEAD", "OPTIONS"}:
            response = client.request(method, _concrete_path(route))
            assert response.status_code == 401, (method, route.path)
            assert response.json()["error"]["code"] == "authentication_required"


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"roles": []},
        {"roles": ["superuser", "admin"]},
        {"roles": "workspace_admin"},
        {"roles": {"workspace_admin": True}},
        {"roles": [["workspace_admin"]]},
        {"roles": ["viewer"] * 33},
        {"roles": ["x" * 65]},
    ],
    ids=["none", "empty", "unknown", "string", "object", "nested", "oversized", "long-role"],
)
def test_authenticated_callers_without_valid_roles_are_denied_everywhere(
    signing_key: RSAKey, claims: dict[str, Any]
) -> None:
    app = _app(signing_key, "authz_denied_everywhere")
    client = TestClient(app)
    headers = _headers(signing_key, **claims)

    for route in _api_routes(app):
        for method in route.methods - {"HEAD", "OPTIONS"}:
            response = client.request(method, _concrete_path(route), headers=headers)
            assert response.status_code == 403, (method, route.path)
            body = response.json()["error"]
            assert body["code"] == "permission_denied"
            assert body["request_id"] == response.headers["X-Request-ID"]
            assert "capability" not in response.text.lower()


def test_viewer_can_read_but_not_write_or_administer(signing_key: RSAKey) -> None:
    client = TestClient(_app(signing_key, "authz_viewer"))
    headers = _headers(signing_key, roles=["viewer"])

    assert client.get("/api/v1/workspaces/missing", headers=headers).status_code == 404
    for method, path in [
        ("POST", "/api/v1/workspaces"),
        ("POST", "/api/v1/datasets/d/analyses"),
        ("POST", "/api/v1/analyses/a/reports"),
        ("GET", "/api/v1/exports/e/content"),
        ("DELETE", "/api/v1/workspaces/w"),
    ]:
        assert client.request(method, path, headers=headers).status_code == 403, path


def test_analyst_cannot_delete_or_manage_consent(signing_key: RSAKey) -> None:
    client = TestClient(_app(signing_key, "authz_analyst"))
    headers = _headers(signing_key, roles=["analyst"])

    workspace = client.post("/api/v1/workspaces", headers=headers)
    workspace_id = workspace.json()["id"]

    assert workspace.status_code == 201
    assert client.delete(f"/api/v1/workspaces/{workspace_id}", headers=headers).status_code == 403
    consent = client.put(
        f"/api/v1/workspaces/{workspace_id}/consent",
        headers=headers,
        json={"notice_version": "x"},
    )
    assert consent.status_code == 403


def test_request_supplied_authorization_state_is_ignored(signing_key: RSAKey) -> None:
    client = TestClient(_app(signing_key, "authz_spoof"))
    headers = _headers(
        signing_key,
        roles=["viewer"],
        tenant_id="f" * 32,
        capabilities=["data:write", "workspace:admin"],
        scope="data:write workspace:admin",
    ) | {
        "X-Tenant-ID": "f" * 32,
        "X-Roles": "workspace_admin",
        "X-Capabilities": "data:write,workspace:admin",
    }

    response = client.post(
        "/api/v1/workspaces?roles=workspace_admin&capabilities=data:write&tenant_id=" + "f" * 32,
        headers=headers,
        json={"roles": ["workspace_admin"], "tenant_id": "f" * 32},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_cross_tenant_access_remains_not_found_for_privileged_callers(
    signing_key: RSAKey,
) -> None:
    client = TestClient(_app(signing_key, "authz_cross_tenant"))
    owner = _headers(signing_key, "owner", roles=["workspace_admin"])
    intruder = _headers(signing_key, "intruder", roles=["workspace_admin"])
    workspace_id = client.post("/api/v1/workspaces", headers=owner).json()["id"]

    assert client.get(f"/api/v1/workspaces/{workspace_id}", headers=owner).status_code == 200
    assert client.get(f"/api/v1/workspaces/{workspace_id}", headers=intruder).status_code == 404
    assert client.delete(f"/api/v1/workspaces/{workspace_id}", headers=intruder).status_code == 404
    assert client.get(f"/api/v1/workspaces/{workspace_id}", headers=owner).status_code == 200


def test_local_mode_keeps_full_capabilities_explicitly() -> None:
    app = create_app(settings=_settings(isolated_directory_path("authz_local"), auth_mode="local"))
    client = TestClient(app)

    workspace_id = client.post("/api/v1/workspaces").json()["id"]

    assert client.delete(f"/api/v1/workspaces/{workspace_id}").status_code == 204
