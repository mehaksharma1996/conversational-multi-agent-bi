"""Run the repository quality checks with minimal, agent-friendly output.

Each check prints one ``PASS``/``FAIL`` line. The tail of a failing check's output is printed so
a person or an AI agent sees only what it needs to fix, instead of full tool logs::

    python -m scripts.check_all                 # fast: ruff, format, mypy, pytest, evaluations
    python -m scripts.check_all --only pytest   # one or more named checks
    python -m scripts.check_all --full          # adds the OpenAPI gate and the web checks
    python -m scripts.check_all --full --e2e    # also Playwright (slow; CI runs it anyway)
    python -m scripts.check_all --list

Exit status: 0 every selected check passed, 1 at least one failed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "apps" / "web"
DEFAULT_TAIL_LINES = 25


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    cwd: Path = REPO_ROOT
    group: str = "fast"  # fast | full | e2e


def _python(*args: str) -> tuple[str, ...]:
    return (sys.executable, "-m", *args)


def _npm(*args: str) -> tuple[str, ...]:
    return (shutil.which("npm") or "npm", *args)


def build_checks(base_ref: str = "origin/main") -> list[Check]:
    """Mirror the check list in AGENTS.md, ordered cheapest first."""
    return [
        Check("ruff-check", _python("ruff", "check", ".")),
        Check("ruff-format", _python("ruff", "format", "--check", ".")),
        Check("mypy", _python("mypy", "apps", "config", "packages", "scripts", "src", "tests")),
        Check("pytest", _python("pytest", "-q")),
        Check("evaluations", _python("scripts.run_evaluations")),
        Check(
            "openapi-compat",
            _python("scripts.check_openapi_compatibility", "--base-ref", base_ref),
            group="full",
        ),
        Check("web-lint", _npm("run", "lint"), WEB_DIR, "full"),
        Check("web-typecheck", _npm("run", "typecheck"), WEB_DIR, "full"),
        Check("web-test", _npm("run", "test"), WEB_DIR, "full"),
        Check("web-build", _npm("run", "build"), WEB_DIR, "full"),
        Check("web-e2e", _npm("run", "test:e2e"), WEB_DIR, "e2e"),
    ]


def select_checks(
    checks: Sequence[Check],
    *,
    only: Sequence[str] = (),
    full: bool = False,
    e2e: bool = False,
) -> list[Check]:
    if only:
        known = {check.name for check in checks}
        unknown = sorted(set(only) - known)
        if unknown:
            raise ValueError(f"Unknown check(s): {', '.join(unknown)}. Known: {sorted(known)}")
        return [check for check in checks if check.name in only]
    groups = {"fast"} | ({"full"} if full else set()) | ({"e2e"} if e2e else set())
    return [check for check in checks if check.group in groups]


def run_check(check: Check, tail_lines: int = DEFAULT_TAIL_LINES) -> tuple[bool, float, str]:
    """Run one check. Return ``(passed, seconds, output_tail)``; the tail is empty on success."""
    started = monotonic()
    try:
        completed = subprocess.run(
            check.command,
            cwd=check.cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return False, monotonic() - started, f"Could not start {check.command[0]}: {error}"
    elapsed = monotonic() - started
    if completed.returncode == 0:
        return True, elapsed, ""
    combined = (completed.stdout + completed.stderr).strip().splitlines()
    return False, elapsed, "\n".join(combined[-tail_lines:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="+", default=[], help="Run only these named checks.")
    parser.add_argument("--full", action="store_true", help="Add the OpenAPI and web checks.")
    parser.add_argument("--e2e", action="store_true", help="Add the Playwright browser tests.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop at the first failure.")
    parser.add_argument("--base-ref", default="origin/main", help="Ref for the OpenAPI gate.")
    parser.add_argument("--tail", type=int, default=DEFAULT_TAIL_LINES, help="Lines to show.")
    parser.add_argument("--list", action="store_true", help="List check names and exit.")
    args = parser.parse_args(argv)

    all_checks = build_checks(args.base_ref)
    if args.list:
        for check in all_checks:
            print(f"{check.name:16} [{check.group}]  {' '.join(check.command[1:])}")
        return 0
    try:
        selected = select_checks(all_checks, only=args.only, full=args.full, e2e=args.e2e)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    failed: list[str] = []
    for check in selected:
        passed, elapsed, tail = run_check(check, args.tail)
        print(f"{'PASS' if passed else 'FAIL'} {check.name} ({elapsed:.1f}s)", flush=True)
        if not passed:
            failed.append(check.name)
            print(tail)
            if args.fail_fast:
                break
    print(
        f"{len(selected) - len(failed)}/{len(selected)} passed"
        + (f"; failed: {failed}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
