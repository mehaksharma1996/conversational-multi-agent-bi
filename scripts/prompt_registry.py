"""Prompt and model change registry: compute, check, and print (issue #18).

The registry (``evals/v1/prompt-registry.json``) records the SHA-256 of every function that builds a
model prompt, the approved default model, and who approved each change and why. A prompt edit
changes its hash, so the test suite fails until a maintainer records a new version with a reason
and an approval and re-runs the offline evaluation
(docs/governance/model-and-prompt-change-policy.md).

    python -m scripts.prompt_registry            # print the current hashes as JSON
    python -m scripts.prompt_registry --check    # exit 1 if the registry is out of date
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "evals" / "v1" / "prompt-registry.json"


def resolve(target: str) -> Any:
    """Import ``module:Qualified.name`` and return the object."""
    module_name, _, qualified = target.partition(":")
    value: Any = importlib.import_module(module_name)
    for part in qualified.split("."):
        value = getattr(value, part)
    return value


def source_hash(target: str) -> str:
    """SHA-256 of the target's source, with line endings and trailing whitespace normalised."""
    source = inspect.getsource(resolve(target))
    normalised = "\n".join(line.rstrip() for line in source.replace("\r\n", "\n").split("\n"))
    return hashlib.sha256(normalised.strip().encode("utf-8")).hexdigest()


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    registry: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return registry


def stale_prompts(registry: dict[str, Any]) -> list[str]:
    """Registry ids whose recorded hash no longer matches the source."""
    return [
        entry["id"]
        for entry in registry["prompts"]
        if source_hash(entry["target"]) != entry["sha256"]
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the registry is stale")
    args = parser.parse_args(argv)
    registry = load_registry()
    if args.check:
        stale = stale_prompts(registry)
        for prompt_id in stale:
            print(f"STALE: {prompt_id} changed without a registry update")
        return 1 if stale else 0
    print(
        json.dumps(
            {entry["id"]: source_hash(entry["target"]) for entry in registry["prompts"]},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
