"""The prompt and model change registry is current, approved, and complete (issue #18).

A prompt edit changes its hash and fails these tests until a maintainer records a new version with a
reason and an approval and re-runs the offline evaluation. See
docs/governance/model-and-prompt-change-policy.md.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import re
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from config.settings import get_settings
from scripts.prompt_registry import (
    FINGERPRINT_COMPONENTS,
    fingerprint_components,
    load_registry,
    source_hash,
    stale_prompts,
)
from scripts.prompt_registry import main as prompt_registry_main

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = load_registry()
# Whole-module targets (``package.module``, no ``:``) hash the entire module (INV-8 V10).
MODULE_TARGETS = {entry["target"] for entry in REGISTRY["prompts"] if ":" not in entry["target"]}
# Prompt modules deliberately not registered, each with the reason and its follow-up. Empty since
# #185 registered the INV-6 planning prompt; a new exclusion needs its own reviewed change.
UNREGISTERED_PROMPT_MODULES: dict[str, str] = {}
PLANNING_TARGET = "src.orchestration.planning_prompt"


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
    assert {
        "src.orchestration.review_prompts",
        "src.orchestration.report_prompt",
        PLANNING_TARGET,
    } <= renderers
    assert UNREGISTERED_PROMPT_MODULES == {}
    missing = renderers - MODULE_TARGETS
    assert not missing, f"register these prompt modules as whole-module targets: {sorted(missing)}"


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


# ---- #185: the planning prompt's complete provenance fingerprint ------------------------------


def _planning_entry(registry: dict[str, Any] = REGISTRY) -> dict[str, Any]:
    entry: dict[str, Any] = next(
        item for item in registry["prompts"] if item["target"] == PLANNING_TARGET
    )
    return entry


def _fresh_planning_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Re-execute planning_prompt.py as the importable module, as a new process would.

    ``_BASE_INSTRUCTIONS`` is evaluated at import time, so a contract change only shows up in a
    fresh import. ``monkeypatch`` restores the original module object afterwards.
    """
    spec = importlib.util.spec_from_file_location(
        PLANNING_TARGET, ROOT / "src" / "orchestration" / "planning_prompt.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, PLANNING_TARGET, module)
    spec.loader.exec_module(module)
    return module


def test_the_planning_prompt_has_a_complete_fingerprint() -> None:
    entry = _planning_entry()

    assert PLANNING_TARGET in MODULE_TARGETS  # whole-module source, not a function hash
    assert set(entry["fingerprint"]) == {"instructions", "sample"}
    for value in entry["fingerprint"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", value)
    assert entry["fingerprint"] == fingerprint_components(PLANNING_TARGET)
    assert set(FINGERPRINT_COMPONENTS) == {PLANNING_TARGET}
    for other in REGISTRY["prompts"]:
        if other["target"] != PLANNING_TARGET:
            assert "fingerprint" not in other


def test_a_missing_or_partial_fingerprint_is_stale() -> None:
    entry = _planning_entry()
    without = {key: value for key, value in entry.items() if key != "fingerprint"}
    partial = {**entry, "fingerprint": {"instructions": entry["fingerprint"]["instructions"]}}

    for tampered in (without, partial):
        assert stale_prompts({**REGISTRY, "prompts": [tampered]}) == [entry["id"]]


def test_a_fingerprint_on_an_entry_without_components_is_stale() -> None:
    entry = next(item for item in REGISTRY["prompts"] if item["target"] != PLANNING_TARGET)
    claimed = {**entry, "fingerprint": {"sample": "0" * 64}}

    assert stale_prompts({**REGISTRY, "prompts": [claimed]}) == [entry["id"]]


def test_editing_the_base_instructions_text_marks_the_planning_prompt_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 1: the module source. The edited text reaches both the source hash and the prompt."""
    path = ROOT / "src" / "orchestration" / "planning_prompt.py"
    original = path.read_text(encoding="utf-8")
    needle = "You do not see rows, values, passages, or results"
    assert needle in original
    edited = original.replace(needle, "You do not see rows or values")
    real_getsource = inspect.getsource

    def getsource(obj: Any) -> str:
        if getattr(obj, "__name__", None) == PLANNING_TARGET:
            return edited
        return real_getsource(obj)

    monkeypatch.setattr("scripts.prompt_registry.inspect.getsource", getsource)

    assert source_hash(PLANNING_TARGET) != _planning_entry()["sha256"]
    assert stale_prompts(REGISTRY) == [_planning_entry()["id"]]


def test_an_interpolated_contract_change_marks_the_planning_prompt_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 2: imported vocabulary. Adding a task kind changes the prompt, not its module source."""
    from packages.investigation import contracts

    proposers: dict[Any, frozenset[contracts.AgentRole]] = dict(contracts.TASK_PROPOSERS)
    executors: dict[Any, frozenset[contracts.AgentRole]] = dict(contracts.TASK_EXECUTORS)
    proposers["forecast"] = frozenset({contracts.AgentRole.SUPERVISOR})
    executors["forecast"] = frozenset({contracts.AgentRole.SQL_ANALYST})
    monkeypatch.setattr(contracts, "TASK_PROPOSERS", proposers)
    monkeypatch.setattr(contracts, "TASK_EXECUTORS", executors)
    module = _fresh_planning_module(monkeypatch)

    assert '- kind "forecast"' in module.INITIAL_INSTRUCTIONS
    assert source_hash(PLANNING_TARGET) == _planning_entry()["sha256"]  # a source hash misses it
    assert (
        fingerprint_components(PLANNING_TARGET)["instructions"]
        != (_planning_entry()["fingerprint"]["instructions"])
    )
    assert stale_prompts(REGISTRY) == [_planning_entry()["id"]]


def test_a_projection_change_marks_the_planning_prompt_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path 3: rendering logic. Unsorted schema fields change only the rendered sample."""
    module = importlib.import_module(PLANNING_TARGET)
    real_project = module._project_metadata

    def unsorted(metadata: Any) -> dict[str, Any]:
        projected: dict[str, Any] = real_project(metadata)
        projected["schema_fields"] = list(reversed(projected["schema_fields"]))
        return projected

    monkeypatch.setattr(module, "_project_metadata", unsorted)
    components = fingerprint_components(PLANNING_TARGET)

    assert components["instructions"] == _planning_entry()["fingerprint"]["instructions"]
    assert components["sample"] != _planning_entry()["fingerprint"]["sample"]
    assert stale_prompts(REGISTRY) == [_planning_entry()["id"]]


def test_the_check_command_fails_on_a_stale_fingerprint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = _planning_entry()
    tampered = {**entry, "fingerprint": {**entry["fingerprint"], "sample": "0" * 64}}
    monkeypatch.setattr(
        "scripts.prompt_registry.load_registry",
        lambda: {**REGISTRY, "prompts": [tampered]},
    )

    assert prompt_registry_main(["--check"]) == 1
    assert f"STALE: {entry['id']}" in capsys.readouterr().out
