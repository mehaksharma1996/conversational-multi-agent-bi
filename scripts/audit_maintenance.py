"""Seal, list, and archive audit segments without breaking the hash chain (issue #18).

    python -m scripts.audit_maintenance status  [DIR]
    python -m scripts.audit_maintenance rotate  [DIR] --tenant TENANT
    python -m scripts.audit_maintenance prune   [DIR] --archive-dir D --keep-segments N

``prune`` moves all but the newest N sealed segments into the archive directory and records
an anchor, so the retained chain still verifies with ``scripts.verify_audit``. Copy the archive
off-host: the anchor says where the retained chain starts, not what came before. Run it while
the API is stopped or idle; it is not coordinated with a running writer. It never deletes a
record.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from packages.governance import JsonlAuditSink

_TENANT = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _tenants(directory: Path) -> list[str]:
    names = {
        path.name.split(".")[0]
        for pattern in ("*.jsonl", "*.anchor.json")
        for path in directory.glob(pattern)
    }
    return sorted(name for name in names if _TENANT.fullmatch(name))


def _chain_ok(sink: JsonlAuditSink, tenant: str) -> bool:
    try:
        return sink.verify(tenant)
    except (ValueError, KeyError, OSError):
        return False  # unreadable or malformed content is a broken chain


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["status", "rotate", "prune"])
    parser.add_argument("directory", nargs="?", type=Path, default=None)
    parser.add_argument("--tenant", help="limit to one tenant (required for rotate)")
    parser.add_argument("--archive-dir", type=Path, help="where prune moves old segments")
    parser.add_argument("--keep-segments", type=int, default=None)
    args = parser.parse_args(argv)

    directory = args.directory
    if directory is None:
        from config.settings import get_settings

        directory = get_settings().audit_dir
    if not directory.is_dir():
        print(f"Audit directory not found: {directory}", file=sys.stderr)
        return 2
    sink = JsonlAuditSink(directory)
    tenants = [args.tenant] if args.tenant else _tenants(directory)

    if args.command == "status":
        for tenant in tenants:
            ok = _chain_ok(sink, tenant)
            records = len(sink.read(tenant)) if ok else "unreadable"
            print(
                f"{tenant}: {len(sink.segments(tenant))} sealed segment(s), "
                f"{records} record(s), chain {'OK' if ok else 'BROKEN'}"
            )
        return 0
    if args.command == "rotate":
        if not args.tenant:
            print("rotate needs --tenant.", file=sys.stderr)
            return 2
        sealed = sink.rotate(args.tenant)
        print(f"Sealed {sealed.name}." if sealed else "Nothing to seal.")
        return 0
    if args.archive_dir is None or args.keep_segments is None:
        print("prune needs --archive-dir and --keep-segments.", file=sys.stderr)
        return 2
    for tenant in tenants:
        if not _chain_ok(sink, tenant):
            print(f"{tenant}: chain is BROKEN; not pruning.", file=sys.stderr)
            return 1
        moved = sink.prune(tenant, keep_segments=args.keep_segments, archive_dir=args.archive_dir)
        chain = "OK" if _chain_ok(sink, tenant) else "BROKEN"
        print(f"{tenant}: archived {len(moved)} segment(s); chain {chain}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
