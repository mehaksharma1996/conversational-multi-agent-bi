"""Audit rotation keeps one verifiable hash chain across segments, restarts, and pruning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.governance import AuditRecorder, JsonlAuditSink
from scripts.audit_maintenance import main as maintenance
from scripts.verify_audit import main as verify_main
from scripts.verify_audit import verify_directory
from tests.test_utils import isolated_directory_path

TENANT = "a" * 32
OTHER = "b" * 32
SMALL = 400  # bytes: a few records per segment


def _write(sink: JsonlAuditSink, count: int, tenant: str = TENANT) -> None:
    recorder = AuditRecorder(sink)
    for _ in range(count):
        recorder.record("workspace.expired", tenant_id=tenant, reason="retention")


def _fresh(name: str, max_bytes: int | None = SMALL) -> tuple[Path, JsonlAuditSink]:
    root = isolated_directory_path(name)
    return root, JsonlAuditSink(root, max_segment_bytes=max_bytes)


def test_a_full_active_file_is_sealed_and_the_chain_continues_across_segments() -> None:
    root, sink = _fresh("audit_rotate")

    _write(sink, 12)

    segments = sink.segments(TENANT)
    assert len(segments) >= 2
    assert segments[0].name == f"{TENANT}.000001.jsonl"
    records = sink.read(TENANT)
    assert [r["sequence"] for r in records] == list(range(1, 13))
    assert sink.verify(TENANT)
    # Each sealed segment is itself bounded by the limit plus at most one record.
    assert all(path.stat().st_size < SMALL * 2 for path in segments)
    assert (root / f"{TENANT}.jsonl").exists()


def test_the_chain_continues_after_a_restart_in_every_rotation_state() -> None:
    root, sink = _fresh("audit_restart")
    _write(sink, 8)
    sealed_last = sink.rotate(TENANT)  # active file empty: the head must come from a sealed segment
    assert sealed_last is not None
    assert not (root / f"{TENANT}.jsonl").exists()

    restarted = JsonlAuditSink(root, max_segment_bytes=SMALL)
    _write(restarted, 3)

    records = restarted.read(TENANT)
    assert [r["sequence"] for r in records] == list(range(1, 12))
    assert restarted.verify(TENANT)


def test_a_new_tenant_starts_at_genesis_and_tenants_never_share_segments() -> None:
    _, sink = _fresh("audit_tenants")
    _write(sink, 6)
    _write(sink, 2, OTHER)

    assert [r["sequence"] for r in sink.read(OTHER)] == [1, 2]
    assert sink.segments(OTHER) == []
    assert all(OTHER not in path.name for path in sink.segments(TENANT))
    assert sink.verify(TENANT) and sink.verify(OTHER)


def test_tampering_with_a_sealed_segment_is_detected() -> None:
    _, sink = _fresh("audit_tamper")
    _write(sink, 12)
    first = sink.segments(TENANT)[0]
    lines = first.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["attributes"] = {"reason": "edited"}
    lines[0] = json.dumps(record, sort_keys=True)
    first.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not sink.verify(TENANT)


def test_removing_a_middle_segment_without_an_anchor_is_detected() -> None:
    _, sink = _fresh("audit_gap")
    _write(sink, 16)
    segments = sink.segments(TENANT)
    assert len(segments) >= 3
    segments[1].unlink()

    assert not sink.verify(TENANT)


def test_pruning_archives_old_segments_and_the_retained_chain_still_verifies() -> None:
    root, sink = _fresh("audit_prune")
    _write(sink, 20)
    before = sink.read(TENANT)
    segments = sink.segments(TENANT)
    archive = root.parent / (root.name + "-archive")

    moved = sink.prune(TENANT, keep_segments=1, archive_dir=archive)

    assert len(moved) == len(segments) - 1
    assert sorted(path.name for path in archive.iterdir()) == [p.name for p in moved]
    assert (root / f"{TENANT}.anchor.json").exists()
    retained = sink.read(TENANT)
    assert retained == before[len(before) - len(retained) :]
    assert sink.verify(TENANT)
    # Writing continues, and a restart re-reads the anchor and active file correctly.
    _write(sink, 2)
    again = JsonlAuditSink(root, max_segment_bytes=SMALL)
    assert again.verify(TENANT)
    assert again.read(TENANT)[-1]["sequence"] == before[-1]["sequence"] + 2


def test_pruning_everything_keeps_the_head_so_new_events_still_chain() -> None:
    root, sink = _fresh("audit_prune_all")
    _write(sink, 10)
    sink.rotate(TENANT)

    sink.prune(TENANT, keep_segments=0, archive_dir=root.parent / (root.name + "-archive"))
    fresh = JsonlAuditSink(root, max_segment_bytes=SMALL)
    _write(fresh, 2)

    assert [r["sequence"] for r in fresh.read(TENANT)] == [11, 12]
    assert fresh.verify(TENANT)


def test_an_unsafe_prune_request_is_rejected() -> None:
    root, sink = _fresh("audit_prune_bad")
    _write(sink, 12)

    with pytest.raises(ValueError, match="negative"):
        sink.prune(TENANT, keep_segments=-1, archive_dir=root / "x")
    assert sink.prune(TENANT, keep_segments=99, archive_dir=root / "x") == []


def test_pruning_never_overwrites_an_existing_archive_file() -> None:
    root, sink = _fresh("audit_prune_collide")
    _write(sink, 16)
    archive = root.parent / (root.name + "-archive")
    archive.mkdir()
    (archive / sink.segments(TENANT)[0].name).write_text("precious", encoding="utf-8")

    with pytest.raises(Exception, match="already exists"):
        sink.prune(TENANT, keep_segments=1, archive_dir=archive)

    assert (archive / sink.segments(TENANT)[0].name).read_text(encoding="utf-8") == "precious"


def test_rotation_is_off_by_default_and_a_single_file_chain_is_unchanged() -> None:
    root, sink = _fresh("audit_off", max_bytes=None)
    _write(sink, 30)

    assert sink.segments(TENANT) == []
    assert sorted(path.name for path in root.iterdir()) == [f"{TENANT}.jsonl"]
    assert sink.verify(TENANT)
    with pytest.raises(ValueError, match="positive"):
        JsonlAuditSink(root, max_segment_bytes=0)


def test_the_verifier_and_maintenance_cli_understand_segments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, sink = _fresh("audit_cli")
    _write(sink, 14)
    archive = root.parent / (root.name + "-archive")

    result = verify_directory(root)
    assert result[TENANT]["valid"] is True and result[TENANT]["segments"] >= 2
    assert maintenance(["status", str(root)]) == 0
    assert "chain OK" in capsys.readouterr().out
    assert (
        maintenance(["prune", str(root), "--archive-dir", str(archive), "--keep-segments", "1"])
        == 0
    )
    assert verify_main([str(root), "--require-files"]) == 0
    assert maintenance(["prune", str(root)]) == 2, "prune needs its arguments"
    assert maintenance(["rotate", str(root)]) == 2, "rotate needs a tenant"


def test_the_cli_refuses_to_prune_a_broken_chain(capsys: pytest.CaptureFixture[str]) -> None:
    root, sink = _fresh("audit_cli_broken")
    _write(sink, 14)
    sink.segments(TENANT)[0].write_text("not json\n", encoding="utf-8")

    code = maintenance(
        ["prune", str(root), "--archive-dir", str(root / "a"), "--keep-segments", "0"]
    )

    assert code == 1
    assert "BROKEN" in capsys.readouterr().err
    assert sink.segments(TENANT), "nothing may be moved when the chain is broken"


def test_the_rotation_threshold_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import get_settings

    monkeypatch.delenv("AUDIT_MAX_SEGMENT_BYTES", raising=False)
    assert get_settings().audit_max_segment_bytes is None
    monkeypatch.setenv("AUDIT_MAX_SEGMENT_BYTES", "67108864")
    assert get_settings().audit_max_segment_bytes == 67_108_864
    monkeypatch.setenv("AUDIT_MAX_SEGMENT_BYTES", "0")
    with pytest.raises(ValueError, match="AUDIT_MAX_SEGMENT_BYTES"):
        get_settings()
