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


# ADR 0024: investigation contracts stay framework-neutral so the LangGraph runtime, the API, and
# evaluation can all depend on them without depending on each other.
INVESTIGATION_BANNED_PREFIXES = ("apps", "fastapi", "langchain", "langgraph", "src", "streamlit")


def test_investigation_contracts_are_framework_neutral() -> None:
    package = PACKAGE_ROOT / "investigation"
    violations = [
        f"{path.name} -> {imported_module}"
        for path in sorted(package.rglob("*.py"))
        for imported_module in sorted(_imports(path))
        if imported_module.split(".")[0] in INVESTIGATION_BANNED_PREFIXES
    ]

    assert package.is_dir()
    assert not violations, "Investigation contracts imported a framework: " + ", ".join(violations)
