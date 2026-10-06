"""Audit the governed configuration, including the model, without recording any secret (issue #18).

At startup the API computes a fingerprint of the settings that change what the system does with user
data (model and provider chain, hosted/local mode, authentication mode, storage backend, retention,
feature switches). The first fingerprint is recorded as ``config.recorded``; a later start whose
fingerprint differs records ``config.changed`` with both hashes. Nothing is recorded when nothing
changed. Secrets (API keys, encryption keys, DSNs, client secrets) are never part of the input, and
the hash alone cannot be reversed to recover any value.

Prompt changes are governed in git and in ``evals/v1/prompt-registry.json``
(docs/governance/model-and-prompt-change-policy.md); this module covers runtime configuration.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from packages.connectors import AuditSink
from packages.governance.audit import SYSTEM_TENANT_ID, AuditRecorder

RESOURCE_ID = "configuration"
_CONFIG_EVENTS = {"config.recorded", "config.changed"}


def config_fingerprint(values: Mapping[str, str | bool | int | None]) -> str:
    """Stable 16-hex-character digest of the governed settings (order independent)."""
    canonical = json.dumps(dict(values), sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()[:16]


def last_recorded_hash(sink: AuditSink) -> str | None:
    """The ``config_hash`` of the most recent configuration event, or None if there is none."""
    records: list[Any]
    read = getattr(sink, "read", None)
    for_tenant = getattr(sink, "for_tenant", None)
    if callable(read):
        records = [
            (item.get("name"), item.get("attributes") or {}) for item in read(SYSTEM_TENANT_ID)
        ]
    elif callable(for_tenant):
        records = [(event.name, event.attributes) for event in for_tenant(SYSTEM_TENANT_ID)]
    else:
        return None
    for name, attributes in reversed(records):
        if name in _CONFIG_EVENTS and isinstance(attributes.get("config_hash"), str):
            return str(attributes["config_hash"])
    return None


def record_configuration(
    recorder: AuditRecorder,
    sink: AuditSink,
    values: Mapping[str, str | bool | int | None],
    *,
    llm_provider: str,
    llm_model: str,
    authentication_mode: str,
    local_only_mode: bool,
    durable_metadata: bool,
) -> str | None:
    """Record the configuration if it is new or changed; returns the event name, or None."""
    current = config_fingerprint(values)
    previous = last_recorded_hash(sink)
    if previous == current:
        return None
    attributes: dict[str, str | bool] = {
        "config_hash": current,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "authentication_mode": authentication_mode,
        "local_only_mode": local_only_mode,
        "durable_metadata": durable_metadata,
    }
    name = "config.recorded" if previous is None else "config.changed"
    if previous is not None:
        attributes["previous_config_hash"] = previous
    recorder.record(name, tenant_id=SYSTEM_TENANT_ID, resource_id=RESOURCE_ID, **attributes)
    return name
