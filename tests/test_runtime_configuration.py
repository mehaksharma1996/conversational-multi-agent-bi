"""Regression tests for explicit runtime configuration and import purity."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
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
    for name in tuple(environment):
        if name.casefold() == "python_dotenv_disabled":
            del environment[name]
    inherited = environment.get("PYTHONPATH")
    paths = [str(hook_dir), str(ROOT)]
    if inherited:
        paths.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    return environment


def _run_with_tripwire(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    synthetic = tmp_path / "working-directory"
    synthetic.mkdir()
    (synthetic / ".env").write_text("S0B_CANARY=must-not-be-read\n", encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=synthetic,
        env=_tripwire_environment(tmp_path),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.mark.parametrize(
    "statement",
    [
        "from config.settings import Settings, get_settings; get_settings()",
        "import apps.api; assert 'apps.api.main' not in sys.modules",
        "import apps.api.main",
        "import apps.api.runtime; assert 'apps.api.main' not in sys.modules",
        "import apps.mcp.__main__",
        "import apps.mcp.server",
        "import src.documents.vector_store; assert 'chromadb' not in sys.modules",
    ],
)
def test_repository_imports_do_not_access_dotenv(statement: str, tmp_path: Path) -> None:
    completed = _run_with_tripwire("import sys; " + statement, tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert TRIPWIRE_MESSAGE not in completed.stderr


def test_known_chromadb_import_time_dotenv_access_is_characterized(tmp_path: Path) -> None:
    completed = _run_with_tripwire("import chromadb", tmp_path)

    assert completed.returncode != 0
    assert TRIPWIRE_MESSAGE in completed.stderr


def test_runtime_loader_uses_explicit_file_without_overriding_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config.runtime as runtime

    synthetic = (tmp_path / ".env").resolve()
    synthetic.write_text("S0B_FROM_DOTENV=loaded\nS0B_PROCESS_VALUE=from-file\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "RUNTIME_DOTENV_PATH", synthetic)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("S0B_FROM_DOTENV", raising=False)
    monkeypatch.setenv("S0B_PROCESS_VALUE", "from-process")

    assert runtime.load_runtime_dotenv() is True
    assert os.environ["S0B_FROM_DOTENV"] == "loaded"
    assert os.environ["S0B_PROCESS_VALUE"] == "from-process"


def test_runtime_loader_honors_dotenv_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config.runtime as runtime

    synthetic = (tmp_path / ".env").resolve()
    synthetic.write_text("S0B_DISABLED_CANARY=not-loaded\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "RUNTIME_DOTENV_PATH", synthetic)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "yes")
    monkeypatch.delenv("S0B_DISABLED_CANARY", raising=False)

    assert runtime.load_runtime_dotenv() is False
    assert "S0B_DISABLED_CANARY" not in os.environ


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_data_dir": tmp_path,
        "sqlite_db_path": tmp_path / "sqlite" / "app.db",
        "chroma_persist_dir": tmp_path / "vectorstore",
        "gemini_api_key": None,
        "gemini_model": "gemini-2.5-flash",
        "embedding_model": "all-MiniLM-L6-v2",
        "local_only_mode": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_asgi_runtime_factory_loads_configuration_and_serves_health(tmp_path: Path) -> None:
    from apps.api.runtime import create_runtime_app

    with (
        patch("apps.api.runtime.load_runtime_dotenv") as load,
        patch("apps.api.main.get_settings", return_value=_settings(tmp_path)),
    ):
        application = create_runtime_app()

    load.assert_called_once_with()
    with TestClient(application) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/health/ready").json() == {"status": "ready"}


def test_asgi_runtime_factory_propagates_invalid_configuration(tmp_path: Path) -> None:
    from apps.api.runtime import create_runtime_app

    invalid = _settings(tmp_path, api_auth_mode="unsupported")
    with (
        patch("apps.api.runtime.load_runtime_dotenv"),
        patch("apps.api.main.get_settings", return_value=invalid),
        pytest.raises(ValueError, match="API_AUTH_MODE"),
    ):
        create_runtime_app()
