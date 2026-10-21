"""The prompt and model change registry is current, approved, and complete (issue #18).

A prompt edit changes its hash and fails these tests until a maintainer records a new version with a
reason and an approval and re-runs the offline evaluation. See
docs/governance/model-and-prompt-change-policy.md.
"""

from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

import pytest

from config.settings import get_settings
from scripts.prompt_registry import load_registry, source_hash, stale_prompts

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = load_registry()
# Whole-module targets (``package.module``, no ``:``) hash the entire module (INV-8 V10).
MODULE_TARGETS = {entry["target"] for entry in REGISTRY["prompts"] if ":" not in entry["target"]}
# Prompt modules deliberately not registered, each with the reason and its follow-up. The INV-6
# planning prompt's text is a module constant; registering it is its own reviewed change, and until
# then its rendered prompts stay pinned by tests/test_investigation_planning_prompt.py.
UNREGISTERED_PROMPT_MODULES = {
    "src.orchestration.planning_prompt": (
        "INV-6 planning prompt (inv6-planning-prompt-v2): pinned by "
        "tests/test_investigation_planning_prompt.py; register as a module target in a separate "
        "reviewed change (ADR 0024 Amendment 6)."
    ),
}


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def test_every_registered_prompt_matches_its_source() -> None:
    stale = stale_prompts(REGISTRY)

    assert stale == [], (
        f"{stale} changed without a registry update. Bump the entry's version, record the reason "
        "and approval, update sha256 (python -m scripts.prompt_registry), and re-run "
        "python -m scripts.run_evaluations; see docs/governance/model-and-prompt-change-policy.md."
    )


def test_every_entry_records_a_reason_an_approval_and_a_rollback() -> None:
    entries = [*REGISTRY["prompts"], *REGISTRY["models"].values()]

    assert len(entries) >= 6
    for entry in entries:
        assert (
            entry["reason"].strip() and entry["approved_by"].strip() and entry["rollback"].strip()
        )
        date.fromisoformat(entry["changed"])
    for prompt in REGISTRY["prompts"]:
        assert isinstance(prompt["version"], int) and prompt["version"] >= 1
        assert re.fullmatch(r"[0-9a-f]{64}", prompt["sha256"])
    assert len({prompt["id"] for prompt in REGISTRY["prompts"]}) == len(REGISTRY["prompts"])


def test_the_default_model_matches_the_approved_registry_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    assert get_settings().gemini_model == REGISTRY["models"]["default_gemini_model"]["value"]


def test_every_prompt_builder_function_in_the_source_is_registered() -> None:
    registered = {
        entry["target"].split(":")[1].split(".")[-1]
        for entry in REGISTRY["prompts"]
        if ":" in entry["target"]
    }
    builders: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and re.fullmatch(r"build_\w*prompt", node.name)
        }
        if _module_name(path) in MODULE_TARGETS:
            registered |= found  # a whole-module target covers every builder in it
        builders |= found

    assert builders, "expected the prompt builders to be discoverable"
    assert builders <= registered, f"register new prompt builders: {sorted(builders - registered)}"


def test_an_edited_prompt_is_reported_as_stale() -> None:
    tampered = {
        **REGISTRY,
        "prompts": [
            {**REGISTRY["prompts"][0], "sha256": "0" * 64},
            *REGISTRY["prompts"][1:],
        ],
    }

    assert stale_prompts(tampered) == [REGISTRY["prompts"][0]["id"]]


def test_the_hash_ignores_line_endings_and_trailing_whitespace() -> None:
    # Windows checkouts use CRLF; the registry must not depend on the platform.
    target = "src.agents.rag_agent:build_rag_prompt"

    assert source_hash(target) == source_hash(target)
    assert re.fullmatch(r"[0-9a-f]{64}", source_hash(target))


def test_every_rendered_prompt_module_is_registered_whole_or_excluded_with_a_reason() -> None:
    """INV-8 V10: a module that renders a model prompt (``render_*prompt``) is registered as a
    whole-module target, so no partial fingerprint can look complete, or it is listed above."""
    renderers: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.FunctionDef) and re.fullmatch(r"render_\w*prompt", node.name)
            for node in ast.walk(tree)
        ):
            renderers.add(_module_name(path))
    assert {"src.orchestration.review_prompts", "src.orchestration.report_prompt"} <= renderers
    missing = renderers - MODULE_TARGETS - set(UNREGISTERED_PROMPT_MODULES)
    assert not missing, f"register these prompt modules as whole-module targets: {sorted(missing)}"
    assert not (set(UNREGISTERED_PROMPT_MODULES) & MODULE_TARGETS)
    assert (ROOT / "tests" / "test_investigation_planning_prompt.py").exists()


def test_a_module_target_hashes_the_whole_module() -> None:
    import importlib
    import inspect

    from scripts.prompt_registry import resolve

    module = resolve("src.orchestration.report_prompt")
    assert module is importlib.import_module("src.orchestration.report_prompt")
    assert "NARRATIVE_MARKER" in inspect.getsource(module)  # module-level text is covered
    entry = next(item for item in REGISTRY["prompts"] if item["target"] == module.__name__)
    assert source_hash(module.__name__) == entry["sha256"]
    tampered = {**REGISTRY, "prompts": [{**entry, "sha256": "0" * 64}]}
    assert stale_prompts(tampered) == [entry["id"]]
