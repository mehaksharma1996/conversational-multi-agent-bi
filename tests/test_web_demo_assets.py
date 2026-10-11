"""The browser serves copies of the demo files; they must never drift from the committed sources."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["retail_operations.csv", "retail_operations_policy.pdf"])
def test_served_demo_files_are_byte_identical_to_the_committed_sample_data(name: str) -> None:
    served = ROOT / "apps" / "web" / "public" / "demo" / name
    source = ROOT / "sample_data" / name

    assert served.read_bytes() == source.read_bytes()
