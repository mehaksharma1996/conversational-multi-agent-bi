"""The license policy parser and evaluator, plus an offline check of the committed npm lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_licenses import (
    NPM_LOCK_PATH,
    Policy,
    evaluate,
    npm_licenses,
    stale_exceptions,
)

POLICY = Policy.load()


def _violations(licenses: dict[str, str]) -> list[str]:
    violations, _ = evaluate("python", licenses, POLICY)
    return [v.package for v in violations]


@pytest.mark.parametrize(
    "expression",
    [
        "MIT",
        "Apache-2.0 OR BSD-3-Clause",
        "GPL-3.0-only OR MIT",  # one allowed alternative is enough
        "MPL-2.0 AND (Apache-2.0 OR MIT)",
        "BSD",  # alias
        "Apache License, Version 2.0",  # alias
        "Apache-2.0 WITH LLVM-exception",
    ],
)
def test_allowed_expressions_pass(expression: str) -> None:
    assert _violations({"pkg": expression}) == []


@pytest.mark.parametrize(
    "expression",
    [
        "GPL-3.0-only",
        "AGPL-3.0-or-later",
        "LGPL-3.0",
        "SSPL-1.0",
        "UNKNOWN",
        "Proprietary",
        "MIT AND GPL-3.0-only",  # AND needs every part allowed
    ],
)
def test_disallowed_expressions_fail(expression: str) -> None:
    assert _violations({"pkg": expression}) == ["pkg"]


def test_a_reviewed_exception_admits_only_its_own_package_and_ecosystem() -> None:
    violations, used = evaluate("npm", {"stack-trace": "UNKNOWN", "other": "UNKNOWN"}, POLICY)

    assert used == ["npm:stack-trace"]
    assert [v.package for v in violations] == ["other"]
    python_violations, _ = evaluate("python", {"stack-trace": "UNKNOWN"}, POLICY)
    assert [v.package for v in python_violations] == ["stack-trace"]


def test_exceptions_require_a_reason_and_a_review_date(tmp_path: Path) -> None:
    policy = tmp_path / "policy.toml"
    policy.write_text(
        '[allowed]\nspdx = ["MIT"]\n[[exception]]\necosystem = "npm"\npackage = "x"\n'
        'reason = ""\nreviewed = "2026-10-06"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reason and reviewed"):
        Policy.load(policy)


def test_stale_exceptions_are_reported() -> None:
    assert stale_exceptions(POLICY, {"npm": ["react"], "python": []}) == ["npm:stack-trace"]
    assert stale_exceptions(POLICY, {"npm": ["stack-trace"]}) == []


def test_committed_npm_lock_satisfies_the_policy() -> None:
    licenses = npm_licenses(NPM_LOCK_PATH)
    violations, _ = evaluate("npm", licenses, POLICY)

    assert licenses, "package-lock.json must list packages"
    assert violations == []
