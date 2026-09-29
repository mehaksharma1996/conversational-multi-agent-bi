"""Verify the tamper-evident hash chain of every tenant's audit file.

Use after a restore, an upgrade, or an incident::

    python -m scripts.verify_audit /audit --require-files
    python -m scripts.verify_audit --json

Exit status: 0 every chain is intact, 1 a chain is broken (or none were found
with ``--require-files``), 2 the directory is unreadable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from packages.governance import JsonlAuditSink

_TENANT_FILE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def verify_directory(directory: Path) -> dict[str, dict[str, Any]]:
    """Return ``{tenant_id: {"records": n, "valid": bool}}`` for every audit file."""
    sink = JsonlAuditSink(directory)
    results: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.jsonl")):
        tenant_id = path.stem
        if _TENANT_FILE.fullmatch(tenant_id) is None:
            continue
        try:
            records = len(sink.read(tenant_id))
            valid = sink.verify(tenant_id)
        except (ValueError, KeyError, OSError):
            records, valid = 0, False  # unreadable or malformed content is a broken chain
        results[tenant_id] = {"records": records, "valid": valid}
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=None,
        help="Audit directory (default: AUDIT_LOG_DIR or APP_DATA_DIR/audit).",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    parser.add_argument(
        "--require-files",
        action="store_true",
        help="Fail if no audit files exist (use after a restore).",
    )
    args = parser.parse_args(argv)

    directory = args.directory
    if directory is None:
        from config.settings import get_settings

        directory = get_settings().audit_dir
    if not directory.is_dir():
        results: dict[str, dict[str, Any]] = {}
        if args.require_files:
            print(f"Audit directory not found: {directory}", file=sys.stderr)
            return 2
    else:
        try:
            results = verify_directory(directory)
        except OSError as exc:
            print(f"Cannot read audit directory: {exc.strerror}", file=sys.stderr)
            return 2

    broken = [tenant for tenant, item in results.items() if not item["valid"]]
    if args.json:
        print(json.dumps({"tenants": results, "ok": not broken}, sort_keys=True))
    else:
        for tenant, item in results.items():
            print(f"{tenant}: {'OK' if item['valid'] else 'BROKEN'} ({item['records']} record(s))")
        if not results:
            print("No audit files found.")
    if broken:
        return 1
    if args.require_files and not results:
        print("No audit files found, but --require-files was set.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
