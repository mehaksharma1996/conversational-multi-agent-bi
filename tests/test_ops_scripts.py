"""Operational scripts: audit verification, model prefetch, and the smoke test."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from apps.api.main import create_app
from packages.governance import AuditRecorder, JsonlAuditSink
from packages.observability import InMemoryTelemetrySink
from scripts import compose_smoke, prefetch_model, verify_audit
from src.documents.embedding import EmbeddingError
from tests.test_api_features import _settings
from tests.test_utils import isolated_directory_path

TENANT_A = "a" * 32
TENANT_B = "b" * 32


def _write_audit(directory: Path) -> JsonlAuditSink:
    sink = JsonlAuditSink(directory)
    recorder = AuditRecorder(sink)
    recorder.record("workspace.created", tenant_id=TENANT_A)
    recorder.record("workspace.deleted", tenant_id=TENANT_A, reason="user_requested")
    recorder.record("workspace.created", tenant_id=TENANT_B)
    return sink


# --- verify_audit -------------------------------------------------------------------------


def test_verify_audit_reports_intact_chains_for_every_tenant(capsys: Any) -> None:
    directory = isolated_directory_path("ops_audit_ok")
    _write_audit(directory)

    status = verify_audit.main([str(directory), "--json"])

    output = json.loads(capsys.readouterr().out)
    assert status == 0
    assert output["ok"] is True
    assert output["tenants"][TENANT_A] == {"records": 2, "valid": True}
    assert output["tenants"][TENANT_B] == {"records": 1, "valid": True}


def test_verify_audit_fails_when_a_record_was_edited(capsys: Any) -> None:
    directory = isolated_directory_path("ops_audit_tampered")
    _write_audit(directory)
    path = directory / f"{TENANT_A}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["name"] = "workspace.expired"
    lines[0] = json.dumps(record, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    status = verify_audit.main([str(directory)])

    text = capsys.readouterr().out
    assert status == 1
    assert f"{TENANT_A}: BROKEN" in text
    assert f"{TENANT_B}: OK" in text


def test_verify_audit_treats_garbage_files_as_broken_not_as_a_crash(capsys: Any) -> None:
    directory = isolated_directory_path("ops_audit_garbage")
    (directory / f"{TENANT_A}.jsonl").write_text("this is not json\n", encoding="utf-8")

    assert verify_audit.main([str(directory)]) == 1
    assert f"{TENANT_A}: BROKEN" in capsys.readouterr().out


def test_verify_audit_require_files_distinguishes_empty_and_missing(tmp_path: Path) -> None:
    empty = isolated_directory_path("ops_audit_empty")

    assert verify_audit.main([str(empty)]) == 0
    assert verify_audit.main([str(empty), "--require-files"]) == 1
    assert verify_audit.main([str(tmp_path / "missing"), "--require-files"]) == 2


def test_verify_audit_ignores_files_that_are_not_tenant_audit_logs() -> None:
    directory = isolated_directory_path("ops_audit_ignore")
    _write_audit(directory)
    (directory / "notes with spaces.jsonl").write_text("ignored", encoding="utf-8")
    (directory / "README.txt").write_text("ignored", encoding="utf-8")

    assert set(verify_audit.verify_directory(directory)) == {TENANT_A, TENANT_B}


# --- prefetch_model -----------------------------------------------------------------------


def test_prefetch_loads_the_configured_model_and_reports_revision(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    class FakeEmbedder:
        model_name = "fake-model"
        revision = "abc123"

        def __init__(self, name: str) -> None:
            assert name == "configured-model"

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[0.0, 1.0, 2.0]]

    monkeypatch.setenv("EMBEDDING_MODEL", "configured-model")
    monkeypatch.setattr(prefetch_model, "SentenceTransformerEmbedder", FakeEmbedder)

    assert prefetch_model.main() == 0
    assert "revision abc123, 3 dimensions" in capsys.readouterr().out


def test_prefetch_fails_with_a_clear_message_when_the_model_cannot_load(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    class BrokenEmbedder:
        def __init__(self, name: str) -> None:
            self.model_name, self.revision = name, None

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingError("offline")

    monkeypatch.setattr(prefetch_model, "SentenceTransformerEmbedder", BrokenEmbedder)

    assert prefetch_model.main() == 1
    assert "Could not load the embedding model" in capsys.readouterr().err


# --- compose_smoke against an in-process server ------------------------------------------------


@contextmanager
def _serve(*, local_only: bool) -> Iterator[str]:
    tmp_path = isolated_directory_path("ops_smoke")
    settings = replace(_settings(tmp_path), local_only_mode=local_only, gemini_api_key=None)
    app = create_app(settings=settings, telemetry_sink=InMemoryTelemetrySink())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "the in-process server did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture
def live_local_only_server() -> Iterator[str]:
    with _serve(local_only=True) as url:
        yield url


def test_smoke_script_passes_against_a_real_local_only_server(
    live_local_only_server: str, capsys: Any
) -> None:
    status = compose_smoke.main(
        [
            "--base-url",
            live_local_only_server,
            "--expect-local-only",
            "--timeout",
            "30",
        ]
    )

    output = capsys.readouterr().out
    assert status == 0, output
    assert "chat answered by the deterministic memory route" in output
    assert "PDF report downloads" in output
    assert "Smoke test passed." in output


def test_smoke_script_fails_with_status_one_when_an_expectation_is_not_met(
    capsys: Any,
) -> None:
    with _serve(local_only=False) as url:
        status = compose_smoke.main(["--base-url", url, "--expect-local-only", "--timeout", "30"])

    captured = capsys.readouterr()
    assert status == 1
    assert "SMOKE TEST FAILED: workspace reports local-only mode" in captured.err
    assert "Smoke test passed." not in captured.out


def test_smoke_script_rejects_a_missing_csv_before_touching_the_network(capsys: Any) -> None:
    status = compose_smoke.main(["--base-url", "http://127.0.0.1:9", "--csv", "nope.csv"])

    assert status == 2
    assert "CSV file not found" in capsys.readouterr().err


def test_smoke_script_times_out_cleanly_when_nothing_is_listening(capsys: Any) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    status = compose_smoke.main(["--base-url", f"http://127.0.0.1:{port}", "--timeout", "1"])

    assert status == 1
    assert "not ready within" in capsys.readouterr().err


def test_multipart_builder_produces_a_parseable_body() -> None:
    body, boundary = compose_smoke.multipart_file("file", "data.csv", b"a,b\n1,2\n", "text/csv")

    assert body.startswith(f"--{boundary}".encode())
    assert b'name="file"; filename="data.csv"' in body
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert b"a,b\n1,2\n" in body
