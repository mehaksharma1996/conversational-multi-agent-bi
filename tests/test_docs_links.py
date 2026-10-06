"""Relative links in the documentation must point at files that exist."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
INLINE_CODE = re.compile(r"`[^`\n]*`")


def _markdown_files() -> list[Path]:
    return sorted([*(ROOT / "docs").rglob("*.md"), ROOT / "README.md"])


def _broken_links(path: Path) -> list[str]:
    text = INLINE_CODE.sub("", FENCE.sub("", path.read_text(encoding="utf-8")))
    broken: list[str] = []
    for target in LINK.findall(text):
        if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue  # external URLs, mailto, and same-page anchors
        relative = target.split("#", 1)[0]
        if relative and not (path.parent / relative).resolve().exists():
            broken.append(target)
    return broken


def test_every_relative_documentation_link_resolves() -> None:
    problems = {
        str(path.relative_to(ROOT)): broken
        for path in _markdown_files()
        if (broken := _broken_links(path))
    }

    assert problems == {}, problems


def test_the_checker_actually_detects_a_missing_target(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("[ok](page.md) [gone](missing.md#x) [web](https://x.y) [here](#a)\n")

    assert _broken_links(page) == ["missing.md#x"]
