"""Backup, verification, and restore of durable workspace state."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts.workspace_backup import (
    BackupError,
    create_backup,
    main,
    restore_backup,
    verify_archive,
)
from tests.test_api_durable_metadata import (
    Deployment,
    _create_workspace,
    _upload,
)

Members = dict[str, bytes]


def _seed(deployment: Deployment) -> tuple[str, str]:
    """Create one workspace with an upload and a (valid) vector index file; return their ids."""
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client, key="backup-key")
        upload_id = _upload(client, workspace["id"])
        directory = next((deployment.root / "api").glob("*/ws_*"))
        index = directory / "vectorstore" / "chroma.sqlite3"
        index.parent.mkdir()
        with closing(sqlite3.connect(index, isolation_level=None)) as connection:
            connection.execute("CREATE TABLE embeddings (id INTEGER)")
    return str(workspace["id"]), upload_id


def _read(archive: Path) -> Members:
    with tarfile.open(archive, "r:gz") as handle:
        return {
            member.name: (handle.extractfile(member) or io.BytesIO()).read()
            for member in handle.getmembers()
        }


def _write(archive: Path, members: Members) -> Path:
    with tarfile.open(archive, "w:gz") as handle:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            handle.addfile(info, io.BytesIO(payload))
    return archive


def _mutated(archive: Path, tmp_path: Path, change: Callable[[Members], None]) -> Path:
    members = _read(archive)
    change(members)
    return _write(tmp_path / "mutated.tar.gz", members)


def _manifest(members: Members) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(members["manifest.json"])
    return manifest


def _save_manifest(members: Members, manifest: dict[str, Any]) -> None:
    members["manifest.json"] = json.dumps(manifest).encode("utf-8")


@pytest.fixture
def backup(tmp_path: Path) -> tuple[Deployment, Path, str, str]:
    deployment = Deployment("backup_source")
    workspace_id, upload_id = _seed(deployment)
    archive = tmp_path / "workspaces.tar.gz"
    create_backup(deployment.root / "api", archive)
    return deployment, archive, workspace_id, upload_id


def test_a_backup_verifies_and_restores_into_a_working_deployment(
    backup: tuple[Deployment, Path, str, str],
) -> None:
    deployment, archive, workspace_id, upload_id = backup

    report = verify_archive(archive)
    assert report.ok, report.problems
    assert (report.workspaces, report.uploads) == (1, 1)

    target = Deployment("backup_target")
    restore_backup(archive, target.root / "api")
    with TestClient(target.app()) as client:
        assert client.get(f"/api/v1/workspaces/{workspace_id}").status_code == 200
        dataset = client.post(f"/api/v1/tabular-uploads/{upload_id}/dataset", json={})
        assert dataset.status_code == 201 and dataset.json()["row_count"] == 2
    assert deployment.root != target.root


def test_a_backup_can_be_taken_while_the_api_is_running(tmp_path: Path) -> None:
    deployment = Deployment("backup_online")
    with TestClient(deployment.app()) as client:
        workspace = _create_workspace(client)
        _upload(client, workspace["id"])

        manifest = create_backup(deployment.root / "api", tmp_path / "online.tar.gz")

        assert [w["id"] for w in manifest["workspaces"]] == [workspace["id"]]
        # The service keeps working after a backup.
        assert client.get(f"/api/v1/workspaces/{workspace['id']}").status_code == 200
    assert verify_archive(tmp_path / "online.tar.gz").ok


def test_tampered_file_content_is_detected(
    backup: tuple[Deployment, Path, str, str], tmp_path: Path
) -> None:
    _, archive, *_ = backup

    def change(members: Members) -> None:
        name = next(n for n in members if n.endswith(".bin"))
        members[name] = b"x" + members[name][1:]

    problems = verify_archive(_mutated(archive, tmp_path, change)).problems

    assert any("recorded checksum" in problem for problem in problems)


def test_a_missing_upload_file_is_detected(
    backup: tuple[Deployment, Path, str, str], tmp_path: Path
) -> None:
    _, archive, *_ = backup

    def change(members: Members) -> None:
        name = next(n for n in members if n.endswith(".bin"))
        del members[name]
        manifest = _manifest(members)
        del manifest["files"][name]
        _save_manifest(members, manifest)

    problems = verify_archive(_mutated(archive, tmp_path, change)).problems

    assert any("upload file is missing" in problem for problem in problems)


def test_a_file_under_a_foreign_tenant_is_detected(
    backup: tuple[Deployment, Path, str, str], tmp_path: Path
) -> None:
    _, archive, *_ = backup

    def change(members: Members) -> None:
        name = next(n for n in members if n.endswith(".bin"))
        foreign = name.replace("workspaces/", "workspaces/", 1).split("/")
        foreign[1] = "e" * 32
        moved = "/".join(foreign)
        members[moved] = members.pop(name)
        manifest = _manifest(members)
        manifest["files"][moved] = manifest["files"].pop(name)
        _save_manifest(members, manifest)

    problems = verify_archive(_mutated(archive, tmp_path, change)).problems

    assert any("does not own" in problem for problem in problems)


def test_a_file_that_metadata_does_not_own_is_detected(
    backup: tuple[Deployment, Path, str, str], tmp_path: Path
) -> None:
    _, archive, *_ = backup

    def change(members: Members) -> None:
        name = next(n for n in members if n.endswith(".bin"))
        parts = name.split("/")
        parts[2] = "ws_" + "9" * 32
        stray = "/".join(parts[:3] + ["stray.bin"])
        members[stray] = b"stray"
        manifest = _manifest(members)
        manifest["files"][stray] = {
            "sha256": hashlib.sha256(b"stray").hexdigest(),
            "size": 5,
        }
        _save_manifest(members, manifest)

    problems = verify_archive(_mutated(archive, tmp_path, change)).problems

    assert any("does not own" in problem for problem in problems)


def test_a_corrupt_vector_index_database_is_detected(
    backup: tuple[Deployment, Path, str, str], tmp_path: Path
) -> None:
    _, archive, *_ = backup

    def change(members: Members) -> None:
        name = next(n for n in members if n.endswith("chroma.sqlite3"))
        members[name] = b"garbage" * 200
        manifest = _manifest(members)
        manifest["files"][name] = {
            "sha256": hashlib.sha256(members[name]).hexdigest(),
            "size": len(members[name]),
        }
        _save_manifest(members, manifest)

    problems = verify_archive(_mutated(archive, tmp_path, change)).problems

    assert any("vector index database" in problem for problem in problems)


@pytest.mark.parametrize("name", ["../evil.txt", "/abs/evil.txt", "workspaces/x/y/evil"])
def test_unsafe_archive_members_are_rejected(
    backup: tuple[Deployment, Path, str, str], tmp_path: Path, name: str
) -> None:
    _, archive, *_ = backup

    problems = verify_archive(
        _mutated(archive, tmp_path, lambda members: members.update({name: b"x"}))
    ).problems

    assert any("unsafe path" in problem for problem in problems)


def test_an_archive_from_a_newer_schema_is_refused(
    backup: tuple[Deployment, Path, str, str], tmp_path: Path
) -> None:
    _, archive, *_ = backup

    def change(members: Members) -> None:
        manifest = _manifest(members)
        manifest["schema_version"] = 99
        _save_manifest(members, manifest)

    problems = verify_archive(_mutated(archive, tmp_path, change)).problems

    assert any("newer than this build" in problem for problem in problems)


def test_truncated_and_non_archives_are_reported(
    backup: tuple[Deployment, Path, str, str], tmp_path: Path
) -> None:
    _, archive, *_ = backup
    truncated = tmp_path / "truncated.tar.gz"
    truncated.write_bytes(archive.read_bytes()[:200])
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not an archive")

    assert not verify_archive(truncated).ok
    assert not verify_archive(junk).ok
    assert not verify_archive(tmp_path / "missing.tar.gz").ok


def test_restore_refuses_an_occupied_root_and_never_deletes_it_with_replace(
    backup: tuple[Deployment, Path, str, str], tmp_path: Path
) -> None:
    _, archive, workspace_id, _ = backup
    target = Deployment("backup_occupied")
    existing = _seed(target)[0]

    with pytest.raises(BackupError, match="--replace"):
        restore_backup(archive, target.root / "api")

    restore_backup(archive, target.root / "api", replace=True)
    aside = list(target.root.glob("api.pre-restore-*"))
    assert len(aside) == 1, "the previous state is moved aside, not deleted"
    assert any(aside[0].glob(f"*/{existing}")), "the old workspace is still on disk"
    with TestClient(target.app()) as client:
        assert client.get(f"/api/v1/workspaces/{workspace_id}").status_code == 200


def test_restore_refuses_while_the_api_holds_the_database(
    backup: tuple[Deployment, Path, str, str],
) -> None:
    deployment, archive, *_ = backup
    holder = sqlite3.connect(deployment.metadata_db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(BackupError, match="in use"):
            restore_backup(archive, deployment.root / "api", replace=True)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_restore_rejects_a_bad_archive_before_touching_anything(
    backup: tuple[Deployment, Path, str, str], tmp_path: Path
) -> None:
    _, archive, *_ = backup
    tampered = _mutated(archive, tmp_path, lambda m: m.update({"metadata.db": b"broken"}))
    target = tmp_path / "untouched"

    with pytest.raises(BackupError, match="Refusing to restore"):
        restore_backup(tampered, target)

    assert not target.exists() and not list(tmp_path.glob("untouched.restoring-*"))


def test_backup_refuses_when_there_is_nothing_to_back_up(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="nothing to back up"):
        create_backup(tmp_path / "empty", tmp_path / "out.tar.gz")


def test_the_command_line_reports_success_and_failure(
    backup: tuple[Deployment, Path, str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    deployment, archive, *_ = backup
    output = tmp_path / "cli.tar.gz"

    assert (
        main(["backup", "--storage-root", str(deployment.root / "api"), "--output", str(output)])
        == 0
    )
    assert main(["verify", str(output)]) == 0
    assert "OK: 1 workspace(s), 1 upload(s)" in capsys.readouterr().out
    broken = _mutated(archive, tmp_path, lambda m: m.update({"metadata.db": b"broken"}))
    assert main(["verify", str(broken)]) == 1
    assert main(["restore", str(broken), "--storage-root", str(tmp_path / "x")]) == 1
