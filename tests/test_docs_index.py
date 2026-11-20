"""The documentation index stays complete and its links resolve (HIRE-4, #207)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
INDEX = DOCS / "README.md"
INDEXED_FOLDERS = ("architecture", "operations", "governance", "security")
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def _relative_links(path: Path) -> list[str]:
    return [
        target
        for target in LINK.findall(path.read_text(encoding="utf-8"))
        if not target.startswith(("http://", "https://", "mailto:"))
    ]


def test_every_relative_link_in_the_index_resolves() -> None:
    missing = [
        target
        for target in _relative_links(INDEX)
        if not (INDEX.parent / target).resolve().exists()
    ]

    assert missing == [], f"docs/README.md links to missing files: {missing}"


def test_every_page_in_the_indexed_folders_is_listed() -> None:
    linked = {(INDEX.parent / target).resolve() for target in _relative_links(INDEX)}
    unlisted = [
        page.relative_to(DOCS).as_posix()
        for folder in INDEXED_FOLDERS
        for page in sorted((DOCS / folder).glob("*.md"))
        if page.resolve() not in linked
    ]

    assert unlisted == [], f"add these pages to docs/README.md: {unlisted}"


def test_the_hygiene_inventory_holds_no_local_machine_paths() -> None:
    inventory = (DOCS / "operations" / "repository-hygiene-inventory.md").read_text(
        encoding="utf-8"
    )

    # A drive letter only at a word boundary, so "https://" does not count.
    local_path = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]|/Users/|\\Users\\|/home/")
    assert local_path.search("C:\\Users\\someone") and not local_path.search("https://x")
    assert not local_path.search(inventory)
