"""Back up, verify, and restore durable workspace state (ADR 0022).

    python -m scripts.workspace_backup backup  --storage-root /data/api --output ws.tar.gz
    python -m scripts.workspace_backup verify  ws.tar.gz
    python -m scripts.workspace_backup restore ws.tar.gz --storage-root /data/api [--replace]

An archive holds a consistent copy of the metadata database (SQLite online backup), every file of
every workspace the database knows, and a manifest of SHA-256 digests. ``verify`` and ``restore``
check the manifest, reject unsafe archive members, run SQLite integrity checks on the metadata and
any Chroma index files, and confirm ownership: each file sits under the tenant that its metadata
row names, each upload matches its recorded digest, and nothing is present that metadata does not
own. ``restore`` refuses to touch a storage root while the API holds its database, and never
deletes the previous state: it is moved aside.

Take backups with the API stopped for a clean point-in-time image of workspace files; the metadata
copy is consistent either way. Audit logs are separate (``ops/backup.sh``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from apps.api.metadata_store import SCHEMA_VERSION

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
METADATA_MEMBER = "metadata.db"
WORKSPACE_PREFIX = "workspaces"
METADATA_RELATIVE = Path(".metadata") / "metadata.db"
_TENANT = re.compile(r"[a-f0-9]{32}")
_WORKSPACE = re.compile(r"ws_[a-f0-9]{32}")
_PARENT_OF = {
    "dataset": "upload",
    "analysis": "dataset",
    "report": "analysis",
    "document collection": "workspace",
}
_SQLITE_HEADER = b"SQLite format 3\x00"
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    """The operation cannot proceed safely; the message is operator-facing and content-free."""


@dataclass
class VerificationReport:
    problems: list[str] = field(default_factory=list)
    workspaces: int = 0
    uploads: int = 0
    files: int = 0
    schema_version: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


def _sha256(handle: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _file_digest(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        return _sha256(handle)


def _read_rows(
    database: Path,
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, str]], int]:
    """Workspaces (tenant, id), uploads (id, workspace, tenant, sha256), and the schema version."""
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        workspaces = [
            (str(tenant), str(identifier))
            for tenant, identifier in connection.execute("SELECT tenant_id, id FROM workspaces")
        ]
        uploads = [
            (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in connection.execute("SELECT id, workspace_id, tenant_id, sha256 FROM uploads")
        ]
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        connection.close()
    return workspaces, uploads, int(version or 0)


def _read_resources(database: Path) -> list[tuple[str, str, str, str, str | None]]:
    """(kind, id, workspace, tenant, parent id) for datasets, analyses, and reports (schema v2+)."""
    queries = {
        "dataset": "SELECT id, workspace_id, tenant_id, upload_id FROM datasets",
        "analysis": "SELECT id, workspace_id, tenant_id, dataset_id FROM analyses",
        "report": "SELECT id, workspace_id, tenant_id, analysis_id FROM reports",
        "document collection": "SELECT id, workspace_id, tenant_id, NULL FROM document_collections",
    }
    resources: list[tuple[str, str, str, str, str | None]] = []
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        for kind, query in queries.items():
            try:
                rows = connection.execute(query).fetchall()
            except sqlite3.OperationalError:
                continue  # an older schema without these tables
            resources += [
                (kind, str(r[0]), str(r[1]), str(r[2]), None if r[3] is None else str(r[3]))
                for r in rows
            ]
    finally:
        connection.close()
    return resources


def _is_plain_sqlite(path: Path) -> bool:
    """False for an SQLCipher-encrypted file, which cannot be opened without its key.

    Encrypted databases are still covered by the manifest digest; only their internals cannot
    be checked here.
    """
    with path.open("rb") as handle:
        return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER


def _integrity_problems(database: Path) -> list[str]:
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return ["not a readable SQLite database"]
    return [] if rows == ["ok"] else rows


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or name != path.as_posix():
        return False
    if name in {MANIFEST, METADATA_MEMBER}:
        return True
    parts = path.parts
    return (
        len(parts) >= 4
        and parts[0] == WORKSPACE_PREFIX
        and _TENANT.fullmatch(parts[1]) is not None
        and _WORKSPACE.fullmatch(parts[2]) is not None
    )


# --- backup --------------------------------------------------------------------------------------


def create_backup(storage_root: Path, output: Path) -> dict[str, Any]:
    """Write ``output`` and return its manifest. Raises ``BackupError`` if state is inconsistent."""
    source = storage_root / METADATA_RELATIVE
    if not source.is_file():
        raise BackupError("No metadata database was found; nothing to back up.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ws-backup-") as scratch:
        copy = Path(scratch) / METADATA_MEMBER
        live = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            target = sqlite3.connect(copy)
            try:
                live.backup(target)
            finally:
                target.close()
        finally:
            live.close()
        problems = _integrity_problems(copy)
        if problems:
            raise BackupError("The metadata database failed its integrity check; not backing up.")
        workspaces, _, schema_version = _read_rows(copy)
        entries: dict[str, Path] = {METADATA_MEMBER: copy}
        for tenant_id, workspace_id in sorted(workspaces):
            if _TENANT.fullmatch(tenant_id) is None or _WORKSPACE.fullmatch(workspace_id) is None:
                raise BackupError("Metadata contains an invalid workspace identifier.")
            directory = storage_root / tenant_id / workspace_id
            if directory.is_symlink():
                raise BackupError(
                    "A workspace directory is a symbolic link; refusing to follow it."
                )
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise BackupError(
                        "A workspace contains a symbolic link; refusing to follow it."
                    )
                if path.is_file() and not path.name.endswith(".tmp"):
                    relative = path.relative_to(directory).as_posix()
                    entries[f"{WORKSPACE_PREFIX}/{tenant_id}/{workspace_id}/{relative}"] = path
        files = {}
        for name, path in entries.items():
            digest, size = _file_digest(path)
            files[name] = {"sha256": digest, "size": size}
        manifest = {
            "format": FORMAT_VERSION,
            "schema_version": schema_version,
            "created_at": datetime.now(UTC).isoformat(),
            "workspaces": [{"tenant_id": t, "id": w} for t, w in sorted(workspaces)],
            "files": files,
        }
        temporary = output.with_name(f".{output.name}.tmp")
        with tarfile.open(temporary, "w:gz") as archive:
            payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(payload)
            info.mtime = int(time.time())
            archive.addfile(info, fileobj=_BytesReader(payload))
            for name, path in entries.items():
                member = archive.gettarinfo(str(path), arcname=name)
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                with path.open("rb") as handle:
                    archive.addfile(member, fileobj=handle)
        os.replace(temporary, output)
    return manifest


class _BytesReader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        end = len(self._payload) if size < 0 else self._offset + size
        chunk = self._payload[self._offset : end]
        self._offset += len(chunk)
        return chunk


# --- verify --------------------------------------------------------------------------------------


def verify_archive(archive_path: Path) -> VerificationReport:
    report = VerificationReport()
    try:
        archive = tarfile.open(archive_path, "r:gz")
    except (tarfile.TarError, OSError, EOFError):
        report.problems.append("The archive is not a readable .tar.gz file.")
        return report
    with archive, tempfile.TemporaryDirectory(prefix="ws-verify-") as scratch:
        try:
            members = archive.getmembers()
        except (tarfile.TarError, OSError, EOFError):
            report.problems.append("The archive is truncated or corrupt.")
            return report
        by_name: dict[str, tarfile.TarInfo] = {}
        for member in members:
            if not member.isfile():
                report.problems.append("The archive contains a member that is not a regular file.")
                continue
            if not _safe_member(member.name):
                report.problems.append("The archive contains an unexpected or unsafe path.")
                continue
            by_name[member.name] = member
        manifest = _load_manifest(archive, by_name, report)
        if manifest is None:
            return report
        report.schema_version = int(manifest.get("schema_version", 0))
        if report.schema_version > SCHEMA_VERSION:
            report.problems.append(
                f"The archive has schema v{report.schema_version}, newer than this build "
                f"(v{SCHEMA_VERSION}); upgrade the application before restoring."
            )
        recorded: dict[str, Any] = manifest.get("files", {})
        extras = set(by_name) - set(recorded) - {MANIFEST}
        if extras:
            report.problems.append(f"{len(extras)} archive file(s) are not listed in the manifest.")
        extracted: dict[str, Path] = {}
        for name, expected in recorded.items():
            listed = by_name.get(name)
            if listed is None:
                report.problems.append("A file listed in the manifest is missing from the archive.")
                continue
            handle = archive.extractfile(listed)
            if handle is None:
                report.problems.append("A manifest file could not be read.")
                continue
            keep = name == METADATA_MEMBER or name.endswith(("chroma.sqlite3", "/content.db"))
            digest = hashlib.sha256()
            size = 0
            destination = Path(scratch) / f"{len(extracted)}.bin"
            with handle, destination.open("wb") if keep else _NullWriter() as sink:
                while chunk := handle.read(_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    sink.write(chunk)
            if digest.hexdigest() != expected.get("sha256") or size != expected.get("size"):
                report.problems.append("A file does not match its recorded checksum.")
            if keep:
                extracted[name] = destination
            report.files += 1
        _check_state(manifest, recorded, extracted, report)
    return report


class _NullWriter:
    def __enter__(self) -> _NullWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def write(self, _chunk: bytes) -> None:
        return None


def _load_manifest(
    archive: tarfile.TarFile, by_name: dict[str, tarfile.TarInfo], report: VerificationReport
) -> dict[str, Any] | None:
    member = by_name.get(MANIFEST)
    if member is None or member.size > _MAX_MANIFEST_BYTES:
        report.problems.append("The archive has no usable manifest.")
        return None
    handle = archive.extractfile(member)
    try:
        manifest = json.loads(handle.read().decode("utf-8")) if handle else None
    except (ValueError, UnicodeDecodeError):
        manifest = None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_VERSION:
        report.problems.append("The manifest is unreadable or has an unsupported format.")
        return None
    if not isinstance(manifest.get("files"), dict):
        report.problems.append("The manifest does not list files.")
        return None
    return manifest


def _check_state(
    manifest: dict[str, Any],
    recorded: dict[str, Any],
    extracted: dict[str, Path],
    report: VerificationReport,
) -> None:
    database = extracted.get(METADATA_MEMBER)
    if database is None:
        report.problems.append("The metadata database is missing from the archive.")
        return
    problems = _integrity_problems(database)
    if problems:
        report.problems.append("The metadata database failed its integrity check.")
        return
    workspaces, uploads, _ = _read_rows(database)
    report.workspaces, report.uploads = len(workspaces), len(uploads)
    owner = {workspace_id: tenant_id for tenant_id, workspace_id in workspaces}
    declared = {(w["tenant_id"], w["id"]) for w in manifest.get("workspaces", [])}
    if declared != set(workspaces):
        report.problems.append("The manifest and metadata disagree about which workspaces exist.")
    for tenant_id, workspace_id in workspaces:
        if _TENANT.fullmatch(tenant_id) is None or _WORKSPACE.fullmatch(workspace_id) is None:
            report.problems.append("Metadata contains an invalid workspace identifier.")
    for name in recorded:
        parts = PurePosixPath(name).parts
        if parts[0] != WORKSPACE_PREFIX:
            continue
        if owner.get(parts[2]) != parts[1]:
            report.problems.append(
                "A file belongs to a workspace or tenant that metadata does not own."
            )
    for upload_id, workspace_id, tenant_id, digest in uploads:
        if owner.get(workspace_id) != tenant_id:
            report.problems.append("An upload's tenant does not match its workspace's owner.")
            continue
        entry = recorded.get(
            f"{WORKSPACE_PREFIX}/{tenant_id}/{workspace_id}/uploads/{upload_id}.bin"
        )
        if entry is None or entry.get("sha256") != digest:
            report.problems.append(
                "An upload file is missing or does not match its metadata digest."
            )
    resources = _read_resources(database)
    known_ids = {upload_id for upload_id, *_ in uploads} | {item[1] for item in resources}
    for kind, _, workspace_id, tenant_id, parent_id in resources:
        if owner.get(workspace_id) != tenant_id:
            report.problems.append(f"A {kind} belongs to a tenant that does not own its workspace.")
        if parent_id is not None and parent_id not in known_ids:
            report.problems.append(f"A {kind} refers to a {_PARENT_OF[kind]} that does not exist.")
    for name, path in extracted.items():
        if name.endswith("chroma.sqlite3") and _integrity_problems(path):
            report.problems.append("A vector index database failed its integrity check.")
        if name.endswith("/content.db") and _is_plain_sqlite(path) and _integrity_problems(path):
            report.problems.append("A workspace content database failed its integrity check.")


# --- restore -------------------------------------------------------------------------------------


def restore_backup(archive_path: Path, storage_root: Path, *, replace: bool = False) -> None:
    report = verify_archive(archive_path)
    if not report.ok:
        raise BackupError("Refusing to restore: " + " ".join(sorted(set(report.problems))))
    live = storage_root / METADATA_RELATIVE
    if live.exists():
        try:
            probe = sqlite3.connect(live, timeout=0.2)
            try:
                probe.execute("BEGIN EXCLUSIVE")
                probe.execute("ROLLBACK")
            finally:
                probe.close()
        except sqlite3.OperationalError as exc:
            raise BackupError("The metadata database is in use; stop the API first.") from exc
    occupied = storage_root.exists() and any(storage_root.iterdir())
    if occupied and not replace:
        raise BackupError("The storage root already has data; use --replace to move it aside.")
    storage_root.parent.mkdir(parents=True, exist_ok=True)
    staging = storage_root.with_name(f"{storage_root.name}.restoring-{os.getpid()}")
    if staging.exists():
        raise BackupError("A previous restore left a staging directory; remove it first.")
    staging.mkdir()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                if member.name == MANIFEST:
                    continue
                if member.name == METADATA_MEMBER:
                    target = staging / METADATA_RELATIVE
                else:
                    target = staging.joinpath(*PurePosixPath(member.name).parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(member)
                if handle is None:
                    raise BackupError("An archive member could not be read.")
                with handle, target.open("wb") as sink:
                    shutil.copyfileobj(handle, sink, _CHUNK)
        if _integrity_problems(staging / METADATA_RELATIVE):
            raise BackupError("The restored metadata database failed its integrity check.")
        if occupied:
            aside = storage_root.with_name(
                f"{storage_root.name}.pre-restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            )
            os.replace(storage_root, aside)
        elif storage_root.exists():
            storage_root.rmdir()
        os.replace(staging, storage_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# --- command line --------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="write a verified archive of workspace state")
    backup.add_argument("--storage-root", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="check an archive without changing anything")
    verify.add_argument("archive", type=Path)
    restore = commands.add_parser("restore", help="verify, then restore an archive")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--storage-root", type=Path, required=True)
    restore.add_argument("--replace", action="store_true", help="move existing data aside")
    args = parser.parse_args(argv)

    try:
        if args.command == "backup":
            manifest = create_backup(args.storage_root, args.output)
            print(
                f"Backup written to {args.output}: {len(manifest['workspaces'])} workspace(s), "
                f"{len(manifest['files'])} file(s), schema v{manifest['schema_version']}."
            )
            result = verify_archive(args.output)
            if not result.ok:
                print("Verification of the new archive FAILED:", *result.problems, sep="\n  ")
                return 1
            print("Archive verified.")
            return 0
        if args.command == "verify":
            result = verify_archive(args.archive)
            if result.ok:
                print(
                    f"OK: {result.workspaces} workspace(s), {result.uploads} upload(s), "
                    f"{result.files} file(s), schema v{result.schema_version}."
                )
                return 0
            print("FAILED:", *sorted(set(result.problems)), sep="\n  ")
            return 1
        restore_backup(args.archive, args.storage_root, replace=args.replace)
        print(f"Restored {args.archive} into {args.storage_root}. Start the API to recover state.")
        return 0
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
