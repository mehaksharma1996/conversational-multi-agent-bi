"""The versioned workflow case manifest: which cases exist and what a run can say about each.

The manifest (``evals/baseline/v2/manifest.json``) classifies every live-demo case and every
investigation case exactly once, by category and by *coverage*: whether an offline scripted run can
grade it at all. Coverage labels keep unmeasured cases out of pass rates instead of letting a
scripted fallback answer look like a pass or a failure:

- ``measured``: the scripted model produces a correct output, so the case is graded.
- ``not_covered_by_script``: the product supports the question, but the scripted model has no
  correct output for it; a scripted run says nothing about it.
- ``requires_agent``: the task is defined as dependent steps. This is a limitation of current
  benchmark coverage, not a claim that one SQL query or one agent could never answer it.
- ``requires_live_model``: only a person reading real model output can judge it.
- ``unsupported``: outside the product's scope by design; a good answer declines or stays
  descriptive.

Fixture hashes pin the inputs. Text files are hashed with line endings normalised to LF, because a
Windows checkout with ``core.autocrlf`` rewrites them; binary files are hashed as stored.

This module only validates definitions. Running cases and aggregating results belong elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from packages.evaluation.result_sets import values_equal

MANIFEST_SCHEMA = "workflow-case-manifest-v1"
COVERAGE_LABELS = (
    "measured",
    "not_covered_by_script",
    "requires_agent",
    "requires_live_model",
    "unsupported",
)
CATEGORIES = (
    "sql",
    "rag",
    "hybrid",
    "follow_up",
    "interpretation",
    "safety",
    "exploratory",
    "investigation",
)
HASH_METHODS = ("sha256-lf", "sha256-raw")
# Source case kinds and the categories they may be filed under. Ground-truth cases are filed by
# their expected route, so a table question cannot be reported as a document one.
_CATEGORIES_BY_KIND = {
    "followup": {"follow_up"},
    "policy_interpretation": {"interpretation"},
    "safety": {"safety"},
    "exploratory": {"exploratory"},
    "investigation": {"investigation"},
}


def fixture_sha256(path: Path, method: str) -> str:
    """Hash a fixture: ``sha256-lf`` normalises CRLF to LF first; ``sha256-raw`` hashes bytes."""
    if method not in HASH_METHODS:
        raise ValueError(f"unknown hash method {method!r}")
    data = path.read_bytes()
    if method == "sha256-lf":
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(manifest: Mapping[str, Any], repo_root: Path) -> list[str]:
    """Every problem with the manifest, as readable strings; an empty list means it is valid."""
    problems: list[str] = []
    if manifest.get("schema") != MANIFEST_SCHEMA:
        problems.append(f"schema must be {MANIFEST_SCHEMA!r}")
    if set(manifest.get("coverage_labels", {})) != set(COVERAGE_LABELS):
        problems.append("coverage_labels must define exactly the known labels")
    if set(manifest.get("categories", {})) != set(CATEGORIES):
        problems.append("categories must define exactly the known categories")

    for name, entry in {**manifest.get("fixtures", {}), **manifest.get("sources", {})}.items():
        path = repo_root / entry["path"]
        if not path.is_file():
            problems.append(f"{name}: {entry['path']} does not exist")
            continue
        try:
            actual = fixture_sha256(path, entry["hash"])
        except ValueError as exc:
            problems.append(f"{name}: {exc}")
            continue
        if actual != entry["sha256"]:
            problems.append(f"{name}: {entry['path']} does not match its pinned hash")

    source_cases: dict[str, dict[str, Any]] = {}
    for source_name, entry in manifest.get("sources", {}).items():
        path = repo_root / entry["path"]
        if path.is_file():
            for case in load_json(path)["cases"]:
                if case["id"] in source_cases:
                    problems.append(f"case id {case['id']} appears in more than one source")
                source_cases[case["id"]] = {**case, "_source": source_name}

    entries = manifest.get("cases", [])
    counts = Counter(entry["id"] for entry in entries)
    problems.extend(f"{case_id}: listed {n} times" for case_id, n in counts.items() if n > 1)
    problems.extend(
        f"{case_id}: defined in a source but missing from the manifest"
        for case_id in sorted(set(source_cases) - set(counts))
    )
    for entry in entries:
        problems.extend(_entry_problems(entry, source_cases.get(entry["id"])))
    return problems


def _entry_problems(entry: Mapping[str, Any], case: Mapping[str, Any] | None) -> list[str]:
    case_id = entry["id"]
    if case is None:
        return [f"{case_id}: not defined in any source"]
    problems: list[str] = []
    if entry.get("source") != case["_source"]:
        problems.append(f"{case_id}: source should be {case['_source']!r}")
    category, coverage = entry.get("category"), entry.get("coverage")
    if category not in CATEGORIES:
        problems.append(f"{case_id}: unknown category {category!r}")
    if coverage not in COVERAGE_LABELS:
        problems.append(f"{case_id}: unknown coverage {coverage!r}")
    if not str(entry.get("rationale", "")).strip():
        problems.append(f"{case_id}: a coverage rationale is required")
    kind = str(case.get("kind"))
    allowed = (
        {case.get("expected_route")} if kind == "ground_truth" else _CATEGORIES_BY_KIND.get(kind)
    )
    if not allowed or category not in allowed:
        problems.append(f"{case_id}: category {category!r} does not fit a {kind!r} case")
    if coverage == "measured" and not case.get("scored"):
        problems.append(f"{case_id}: a measured case must be scored in its source")
    if kind in {"exploratory", "investigation"} and coverage == "measured":
        problems.append(f"{case_id}: {kind} cases cannot be measured offline")
    return problems


def has_unique_leader(rows: Sequence[Sequence[Any]], value_index: int = 1) -> bool:
    """Whether the first row of a ranking is strictly ahead of the second (no tie for first).

    A tie would make "the highest" ambiguous and an ordered comparison unstable.
    """
    if len(rows) < 2:
        return len(rows) == 1
    leader, runner_up = rows[0][value_index], rows[1][value_index]
    return not values_equal(leader, runner_up) and leader > runner_up


def causal_markers_in(text: str, markers: Sequence[str]) -> list[str]:
    """The causal phrases (whole words, case-insensitive) that appear in ``text``."""
    return [
        marker
        for marker in markers
        if re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", text, flags=re.IGNORECASE)
    ]
