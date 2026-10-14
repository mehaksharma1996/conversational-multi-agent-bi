"""Security regressions for the subprocess boundary around offline Python tools."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.offline_execution import REPO_ROOT, offline_environment, run_offline_python

TRIPWIRE_MESSAGE = "DOTENV_ACCESS_BLOCKED"
TRIPWIRE = f'''\
import os
import sys

def _block_dotenv(event, arguments):
    if event != "open" or not arguments:
        return
    target = arguments[0]
    if isinstance(target, int):
        return
    try:
        name = os.path.basename(os.fsdecode(target)).casefold()
    except TypeError:
        return
    if name == ".env":
        raise RuntimeError("{TRIPWIRE_MESSAGE}")

sys.addaudithook(_block_dotenv)
'''


def _tripwire_environment(tmp_path: Path) -> dict[str, str]:
    hook_dir = tmp_path / "tripwire"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(TRIPWIRE, encoding="utf-8")
    environment = dict(os.environ)
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(hook_dir) if not inherited else os.pathsep.join((str(hook_dir), inherited))
    )
    return environment


def _run_entrypoint(
    arguments: list[str], tmp_path: Path, *, timeout: int = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *arguments],
        cwd=REPO_ROOT,
        env=_tripwire_environment(tmp_path),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _assert_no_dotenv_access(completed: subprocess.CompletedProcess[str]) -> None:
    assert TRIPWIRE_MESSAGE not in completed.stderr
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_tripwire_blocks_a_synthetic_dotenv_before_contents_are_read(tmp_path: Path) -> None:
    synthetic = tmp_path / "synthetic"
    synthetic.mkdir()
    (synthetic / ".env").write_text("SYNTHETIC_CANARY=must-not-be-read\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-c", "open('.env', encoding='utf-8').read()"],
        cwd=synthetic,
        env=_tripwire_environment(tmp_path),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode != 0
    assert TRIPWIRE_MESSAGE in completed.stderr


def test_offline_environment_preserves_explicit_values_and_builds_pythonpath(
    tmp_path: Path,
) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    environment = offline_environment(
        {"PYTHONPATH": "explicit-path", "EXPLICIT_SETTING": "preserved"},
        isolated_cwd=isolated,
        original_cwd=REPO_ROOT,
        overrides={
            "PYTHON_DOTENV_DISABLED": "0",
            "CONVERSATIONAL_BI_OFFLINE_CWD": "unsafe",
        },
    )

    assert environment["PYTHON_DOTENV_DISABLED"] == "1"
    assert environment["EXPLICIT_SETTING"] == "preserved"
    assert environment["PYTHONPATH"].split(os.pathsep) == [str(REPO_ROOT), "explicit-path"]
    assert Path(environment["CONVERSATIONAL_BI_OFFLINE_CWD"]) == isolated.resolve()


def test_repository_dotenv_loader_is_disabled_before_settings_import(tmp_path: Path) -> None:
    completed = run_offline_python(
        [sys.executable, "-c", "import config.settings"],
        environ=_tripwire_environment(tmp_path),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    _assert_no_dotenv_access(completed)


def test_chromadb_import_and_persistent_client_do_not_access_dotenv(tmp_path: Path) -> None:
    persist_dir = (tmp_path / "chroma").resolve()
    probe = (
        "import chromadb; "
        f"client = chromadb.PersistentClient(path={json.dumps(str(persist_dir))}); "
        "client.get_or_create_collection('offline_probe')"
    )
    completed = run_offline_python(
        [sys.executable, "-c", probe],
        environ=_tripwire_environment(tmp_path),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    _assert_no_dotenv_access(completed)


def test_openapi_generation_runs_behind_tripwire(tmp_path: Path) -> None:
    output = (tmp_path / "openapi.json").resolve()
    completed = _run_entrypoint(["scripts.generate_openapi", "--output", str(output)], tmp_path)

    _assert_no_dotenv_access(completed)
    assert json.loads(output.read_text(encoding="utf-8"))["openapi"]


def test_focused_pytest_wrapper_runs_behind_tripwire(tmp_path: Path) -> None:
    target = (REPO_ROOT / "tests" / "test_openapi_compatibility.py").resolve()
    completed = _run_entrypoint(["scripts.run_pytest", "--target", str(target), "-q"], tmp_path)

    _assert_no_dotenv_access(completed)


def test_deterministic_evaluation_runs_behind_tripwire(tmp_path: Path) -> None:
    output = (tmp_path / "evaluation.json").resolve()
    completed = _run_entrypoint(
        ["scripts.run_evaluations", "--output", str(output)], tmp_path, timeout=300
    )

    _assert_no_dotenv_access(completed)
    assert json.loads(output.read_text(encoding="utf-8"))["mode"] == "deterministic"


def test_benchmark_entrypoint_imports_behind_tripwire(tmp_path: Path) -> None:
    completed = _run_entrypoint(["scripts.run_benchmarks", "--list"], tmp_path)

    _assert_no_dotenv_access(completed)
