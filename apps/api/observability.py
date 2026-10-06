"""API composition of telemetry and audit recording.

Audit events take their tenant from a trusted ``IdentityContext`` (or a stored
server-side record), never from request headers or bodies.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass

from packages.connectors import AuditEvent, AuditSink, IdentityContext
from packages.governance import AuditRecorder
from packages.observability import OperationTracker, Telemetry, TelemetrySink


@dataclass(frozen=True)
class ApiObservability:
    telemetry: Telemetry
    recorder: AuditRecorder
    audit_sink: AuditSink
    telemetry_sink: TelemetrySink

    @classmethod
    def create(cls, telemetry_sink: TelemetrySink, audit_sink: AuditSink) -> ApiObservability:
        telemetry = Telemetry(telemetry_sink)
        return cls(
            telemetry=telemetry,
            recorder=AuditRecorder(audit_sink, telemetry),
            audit_sink=audit_sink,
            telemetry_sink=telemetry_sink,
        )

    def audit(
        self,
        action: str,
        identity: IdentityContext,
        *,
        resource_id: str | None = None,
        **attributes: object,
    ) -> AuditEvent | None:
        return self.recorder.record(
            action,
            tenant_id=identity.tenant_id,
            resource_id=resource_id,
            **attributes,
        )

    def audit_tenant(
        self,
        action: str,
        tenant_id: str,
        *,
        resource_id: str | None = None,
        **attributes: object,
    ) -> AuditEvent | None:
        """Audit with a tenant from trusted server-side state (verified session or subject)."""
        return self.recorder.record(
            action,
            tenant_id=tenant_id,
            resource_id=resource_id,
            **attributes,
        )

    def operation(
        self,
        name: str,
        identity: IdentityContext,
        **attributes: object,
    ) -> AbstractContextManager[OperationTracker]:
        return self.telemetry.operation(name, tenant_id=identity.tenant_id, **attributes)
