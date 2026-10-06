"""Server-owned API authorization policy.

Capabilities are derived only from an already-authenticated :class:`IdentityContext`. Roles come
from a verified token claim and are mapped here; request headers, query strings, and bodies are
never authority for tenant, role, or capability.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from fastapi import Depends, Request

from apps.api.dependencies import get_identity
from apps.api.errors import AuthorizationError
from apps.api.observability import ApiObservability
from packages.connectors import IdentityContext


class Capability(StrEnum):
    WORKSPACE_READ = "workspace:read"
    DATA_WRITE = "data:write"
    ANALYSIS_RUN = "analysis:run"
    REPORT_EXPORT = "report:export"
    WORKSPACE_ADMIN = "workspace:admin"


_VIEWER = frozenset({Capability.WORKSPACE_READ})
_ANALYST = _VIEWER | {Capability.DATA_WRITE, Capability.ANALYSIS_RUN, Capability.REPORT_EXPORT}

ROLE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "viewer": _VIEWER,
    "analyst": frozenset(_ANALYST),
    "workspace_admin": frozenset(_ANALYST | {Capability.WORKSPACE_ADMIN}),
}

LOCAL_AUTHENTICATION_MODE = "local"


def capabilities_for(identity: IdentityContext) -> frozenset[Capability]:
    """Return the capabilities an authenticated identity holds; unknown roles grant nothing."""
    if identity.authentication_mode == LOCAL_AUTHENTICATION_MODE:
        # Explicit, loopback-only development mode is a single trusted user.
        return frozenset(Capability)
    granted: set[Capability] = set()
    for role in identity.roles:
        granted |= ROLE_CAPABILITIES.get(role, frozenset())
    return frozenset(granted)


class CapabilityRequirement:
    """Dependency that denies callers lacking ``capability`` with a safe 403."""

    def __init__(self, capability: Capability) -> None:
        self.capability = capability

    def __call__(
        self,
        request: Request,
        identity: Annotated[IdentityContext, Depends(get_identity)],
    ) -> None:
        if self.capability not in capabilities_for(identity):
            observability: ApiObservability = request.app.state.observability
            observability.audit(
                "authz.denied",
                identity,
                capability=self.capability.value,
                authentication_mode=identity.authentication_mode,
            )
            raise AuthorizationError()


def requires(capability: Capability) -> Any:
    """Route-level declaration: ``@router.get(..., dependencies=[requires(...)])``."""
    return Depends(CapabilityRequirement(capability))
