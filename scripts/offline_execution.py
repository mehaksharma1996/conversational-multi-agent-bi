"""Run offline Python tools without exposing the repository's dotenv file.

ChromaDB 1.5.x asks Pydantic Settings to read ``.env`` from the process working
directory, independently of python-dotenv.  Offline tools therefore need both a
dotenv-disable flag and a working directory that cannot contain the repository's
``.env``.  This module is deliberately standard-library-only so entry points can
import and invoke it before importing application or ChromaDB modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
_ISOLATED_CWD = "CONVERSATIONAL_BI_OFFLINE_CWD"
_ORIGINAL_CWD = "CONVERSATIONAL_BI_ORIGINAL_CWD"


def _set_environment_value(environment: dict[str, str], name: str, value: str) -> None:
    """Set one variable without retaining a differently-cased Windows alias."""
    for existing in tuple(environment):
        if existing.casefold() == name.casefold():
            del environment[existing]
    environment[name] = value


def _set_default_environment_value(environment: dict[str, str], name: str, value: str) -> None:
    if not any(existing.casefold() == name.casefold() for existing in environment):
        environment[name] = value


def offline_environment(
    environ: Mapping[str, str] | None = None,
    *,
    isolated_cwd: Path,
    original_cwd: Path | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an inherited environment with repository imports and dotenv isolation."""
    environment = dict(os.environ if environ is None else environ)
    for name, value in (overrides or {}).items():
        _set_environment_value(environment, name, value)
    inherited_pythonpath = next(
        (value for key, value in environment.items() if key.casefold() == "pythonpath"), ""
    )
    import_paths = [str(REPO_ROOT)]
    if inherited_pythonpath:
        import_paths.append(inherited_pythonpath)
    _set_environment_value(environment, "PYTHONPATH", os.pathsep.join(import_paths))
    _set_environment_value(environment, "PYTHON_DOTENV_DISABLED", "1")
    _set_environment_value(environment, _ISOLATED_CWD, str(isolated_cwd.resolve()))
    _set_environment_value(
        environment,
        _ORIGINAL_CWD,
        str((original_cwd or Path.cwd()).resolve()),
    )
    # Pytest otherwise enumerates the user-wide temp parent on Windows. Keep its numbered
    # directories inside the isolated boundary without overriding an explicit caller choice.
    _set_default_environment_value(environment, "PYTEST_DEBUG_TEMPROOT", str(isolated_cwd))
    return environment


def run_offline_python(
    command: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    original_cwd: Path | None = None,
    overrides: Mapping[str, str] | None = None,
    capture_output: bool = False,
    text: bool = True,
    encoding: str | None = None,
    errors: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an explicit command in a new empty directory without changing this process's CWD."""
    with TemporaryDirectory(prefix="conversational-bi-offline-", ignore_cleanup_errors=True) as tmp:
        isolated_cwd = Path(tmp).resolve()
        environment = offline_environment(
            environ,
            isolated_cwd=isolated_cwd,
            original_cwd=original_cwd,
            overrides=overrides,
        )
        return subprocess.run(
            list(command),
            cwd=isolated_cwd,
            env=environment,
            shell=False,
            capture_output=capture_output,
            text=text,
            encoding=encoding,
            errors=errors,
            check=False,
        )


def ensure_offline_module(module: str) -> None:
    """Re-exec a ``python -m`` entry point before it imports side-effectful modules."""
    isolated = os.environ.get(_ISOLATED_CWD)
    cwd = Path.cwd().resolve()
    if (
        os.environ.get("PYTHON_DOTENV_DISABLED") == "1"
        and isolated is not None
        and Path(isolated).resolve() == cwd
        and not (cwd / ".env").exists()
    ):
        return
    completed = run_offline_python(
        [sys.executable, "-m", module, *sys.argv[1:]],
        original_cwd=cwd,
    )
    raise SystemExit(completed.returncode)


def invocation_path(value: str | Path) -> Path:
    """Resolve a CLI path against the directory from which the offline tool was invoked."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    base = Path(os.environ.get(_ORIGINAL_CWD, Path.cwd())).resolve()
    return (base / path).resolve()
