"""Architecture guardrails for the framework-neutral package layer."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "packages"
BANNED_IMPORT_PREFIXES = ("apps", "fastapi", "src.ui", "streamlit")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_packages_do_not_depend_on_application_frameworks() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        for imported_module in _imports(path):
            if imported_module.startswith(BANNED_IMPORT_PREFIXES):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)} -> {imported_module}")

    assert not violations, "Framework imports crossed the package boundary: " + ", ".join(
        sorted(violations)
    )
