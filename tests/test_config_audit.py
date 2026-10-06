"""Configuration and model changes are audited without recording any secret (issue #18)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from apps.api.main import create_app
from config.settings import Settings
from packages.governance import (
    SYSTEM_TENANT_ID,
    InMemoryAuditSink,
    JsonlAuditSink,
    config_fingerprint,
    last_recorded_hash,
)
from packages.observability import InMemoryTelemetrySink
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_utils import isolated_directory_path

API_KEY = "AIza-this-is-a-test-secret-key"
ENCRYPTION_KEY = bytes(range(32))


def _start(settings: Settings, audit: Any) -> None:
    app = create_app(
        settings=settings,
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=InMemoryTelemetrySink(),
        audit_sink=audit,
    )
    with TestClient(app):
        pass


def _settings_for(root: Path, **overrides: Any) -> Settings:
    return replace(_settings(root), **overrides)


def _system_events(audit: InMemoryAuditSink) -> list[Any]:
    return audit.for_tenant(SYSTEM_TENANT_ID)


def test_the_first_start_records_the_configuration_once() -> None:
    root = isolated_directory_path("config_first")
    audit = InMemoryAuditSink()

    _start(_settings_for(root), audit)

    (event,) = _system_events(audit)
    assert event.name == "config.recorded" and event.resource_id == "configuration"
    assert event.attributes["llm_model"] == "fake-model"
    assert event.attributes["llm_provider"] == "gemini"
    assert len(event.attributes["config_hash"]) == 16


def test_an_unchanged_restart_records_nothing_new() -> None:
    root = isolated_directory_path("config_same")
    audit = InMemoryAuditSink()

    _start(_settings_for(root), audit)
    _start(_settings_for(root), audit)
    _start(_settings_for(root), audit)

    assert [e.name for e in _system_events(audit)] == ["config.recorded"]


def test_a_model_change_is_recorded_with_the_previous_hash() -> None:
    root = isolated_directory_path("config_model")
    audit = InMemoryAuditSink()

    _start(_settings_for(root), audit)
    _start(_settings_for(root, gemini_model="gemini-3-flash"), audit)

    first, second = _system_events(audit)
    assert second.name == "config.changed"
    assert second.attributes["llm_model"] == "gemini-3-flash"
    assert second.attributes["previous_config_hash"] == first.attributes["config_hash"]
    assert second.attributes["config_hash"] != first.attributes["config_hash"]


def test_other_governed_changes_are_recorded_too() -> None:
    for change in (
        {"local_only_mode": True},
        {"llm_providers": ("gemini", "ollama")},
        {"session_retention_hours": 48},
        {"document_index_backend": "pgvector", "postgres_dsn": "postgresql://x"},
        {"api_auth_mode": "oidc"},
    ):
        root = isolated_directory_path("config_other")
        audit = InMemoryAuditSink()
        _start(_settings_for(root), audit)
        try:
            _start(_settings_for(root, **change), audit)
        except ValueError:
            continue  # an invalid combination is rejected before startup; nothing to record
        assert [e.name for e in _system_events(audit)][-1] == "config.changed", change


def test_secrets_never_reach_the_audit_record() -> None:
    root = isolated_directory_path("config_secrets")
    audit = InMemoryAuditSink()
    settings = _settings_for(
        root,
        gemini_api_key=API_KEY,
        sqlite_encryption_key=ENCRYPTION_KEY,
        postgres_dsn="postgresql://user:hunter2@db/x",
    )

    _start(settings, audit)
    _start(replace(settings, gemini_api_key=API_KEY + "-rotated"), audit)

    serialized = json.dumps(
        [(e.name, e.attributes) for e in audit.events], default=str, sort_keys=True
    )
    for secret in (API_KEY, "hunter2", ENCRYPTION_KEY.hex(), "postgresql://"):
        assert secret not in serialized, secret
    assert [e.name for e in _system_events(audit)] == ["config.recorded"], (
        "rotating a secret is not a governed configuration change"
    )


def test_the_history_survives_in_the_hash_chained_file_across_restarts() -> None:
    root = isolated_directory_path("config_chain")
    audit_dir = root / "audit"
    sink = JsonlAuditSink(audit_dir)

    _start(_settings_for(root), sink)
    _start(_settings_for(root, gemini_model="gemini-3-flash"), JsonlAuditSink(audit_dir))
    _start(_settings_for(root, gemini_model="gemini-3-flash"), JsonlAuditSink(audit_dir))

    records = sink.read(SYSTEM_TENANT_ID)
    assert [r["name"] for r in records] == ["config.recorded", "config.changed"]
    assert sink.verify(SYSTEM_TENANT_ID)
    assert last_recorded_hash(sink) == records[-1]["attributes"]["config_hash"]
    assert (audit_dir / f"{SYSTEM_TENANT_ID}.jsonl").exists()


def test_the_fingerprint_is_stable_and_order_independent() -> None:
    first = config_fingerprint({"a": "1", "b": True, "c": None})
    second = config_fingerprint({"c": None, "b": True, "a": "1"})

    assert first == second and len(first) == 16
    assert config_fingerprint({"a": "2", "b": True, "c": None}) != first
    assert last_recorded_hash(InMemoryAuditSink()) is None
