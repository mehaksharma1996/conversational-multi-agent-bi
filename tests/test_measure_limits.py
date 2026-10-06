"""Smoke tests for the limits measurement helpers (no timing or memory budgets are asserted)."""

from __future__ import annotations

import csv
import io
import time

from scripts import measure_limits
from scripts.measure_limits import MB, PeakSampler, current_rss_bytes


def test_current_rss_is_positive() -> None:
    rss = current_rss_bytes()
    assert rss is not None and rss > 10 * MB


def test_peak_sampler_observes_memory_growth() -> None:
    with PeakSampler() as sampler:
        block = b"\x01" * (96 * MB)
        assert len(block) == 96 * MB
        time.sleep(0.1)
    assert sampler.peak >= sampler.baseline
    assert sampler.delta_mb >= 48


def test_csv_payload_is_deterministic_and_shaped() -> None:
    first = measure_limits._csv_payload(50, labelled=True)
    assert first == measure_limits._csv_payload(50, labelled=True)
    rows = list(csv.reader(io.StringIO(first.decode("utf-8"))))
    assert len(rows) == 51
    assert rows[0][-1] == "label"
    assert {row[-1] for row in rows[1:]} <= {"0", "1"}


def test_probe_listing_marks_real_embedder_probes_opt_in(capsys) -> None:
    assert measure_limits.main(["--list"]) == 0
    listing = capsys.readouterr().out
    assert "analysis_request/rows=500000" in listing
    real = [line for line in listing.splitlines() if "embedder=real" in line]
    assert real and all("--real-embedder" in line for line in real)


def test_unknown_child_probe_is_rejected(capsys) -> None:
    assert measure_limits.run_child("nope") == 2
    assert "Unknown probe" in capsys.readouterr().err


def test_report_rendering_handles_ok_and_failed_probes() -> None:
    report: dict[str, object] = {
        "generated_note": "test",
        "results": [
            {
                "name": "x",
                "status": "ok",
                "size": 5,
                "unit": "rows",
                "total_s": 1.0,
                "peak_rss_mb": 10.0,
                "rss_growth_mb": 2.0,
            },
            {"name": "y", "status": "timeout"},
        ],
    }
    rendered = measure_limits.render_markdown(report)
    assert "| x | 5 rows | 1.0 | 10.0 | 2.0 |" in rendered
    assert "timeout" in rendered
