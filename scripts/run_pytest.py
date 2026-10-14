"""Run pytest in the repository's dotenv-isolated offline subprocess boundary.

Use ``--target`` for focused paths. Other arguments are forwarded to pytest::

    python -m scripts.run_pytest -q
    python -m scripts.run_pytest --target tests/test_settings.py -q

An arbitrary direct ``python -m pytest`` invocation is not protected by this wrapper.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.offline_execution import REPO_ROOT, invocation_path, run_offline_python


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--target", action="append", type=invocation_path, default=[])
    args, pytest_args = parser.parse_known_args(argv)
    targets = args.target or [REPO_ROOT / "tests"]
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str((REPO_ROOT / "pytest.ini").resolve()),
        *pytest_args,
        *(str(Path(target).resolve()) for target in targets),
    ]
    completed = run_offline_python(
        command,
        overrides={"COVERAGE_RCFILE": str((REPO_ROOT / "pyproject.toml").resolve())},
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
