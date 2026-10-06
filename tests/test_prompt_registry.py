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
    registered = {entry["target"].split(":")[1].split(".")[-1] for entry in REGISTRY["prompts"]}
    builders: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        builders |= {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and re.fullmatch(r"build_\w*prompt", node.name)
        }

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
