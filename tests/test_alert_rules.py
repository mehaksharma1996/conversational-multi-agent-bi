"""The shipped alert rules only reference metric families the API can actually expose."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_utils import isolated_directory_path

RULES = Path(__file__).resolve().parents[1] / "ops" / "alerts" / "bi-api.rules.yml"
METRIC = re.compile(r"\bbi_[a-z0-9_]+")
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")
# Counter and histogram families are created on first use, so they are listed from the docs table
# in docs/operations/slos.md; gauges are registered by the application and read from /metrics.
DOCUMENTED = Path(__file__).resolve().parents[1] / "docs" / "operations" / "slos.md"


def _documented_families() -> set[str]:
    return set(re.findall(r"^\| `(bi_[a-z0-9_]+)` \|", DOCUMENTED.read_text("utf-8"), re.MULTILINE))


def _rules_text() -> str:
    return RULES.read_text("utf-8")


def _exposed_gauges() -> set[str]:
    settings = replace(_settings(isolated_directory_path("alert_rules")), metrics_enabled=True)
    app = create_app(
        settings=settings,
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
    )
    with TestClient(app) as client:
        text = client.get("/metrics").text
    return set(re.findall(r"^# TYPE (bi_[a-z0-9_]+) gauge$", text, re.MULTILINE))


def test_every_metric_in_the_rules_is_documented_or_an_exposed_gauge() -> None:
    known = _documented_families() | _exposed_gauges()
    referenced = set()
    for name in METRIC.findall(_rules_text()):
        for suffix in HISTOGRAM_SUFFIXES:
            if name.endswith(suffix) and name[: -len(suffix)] in known:
                name = name[: -len(suffix)]
                break
        referenced.add(name)

    assert referenced, "the rules file should reference at least one metric"
    assert referenced <= known, sorted(referenced - known)


def test_every_alert_has_a_severity_summary_and_runbook() -> None:
    blocks = re.split(r"^      - alert: ", _rules_text(), flags=re.MULTILINE)[1:]

    assert len(blocks) >= 6
    for block in blocks:
        name = block.splitlines()[0].strip()
        assert re.search(r"^\s+expr:", block, re.MULTILINE), name
        assert re.search(r"^\s+severity: (page|ticket)$", block, re.MULTILINE), name
        assert re.search(r"^\s+summary: \S", block, re.MULTILINE), name
        runbook = re.search(r"^\s+runbook: (\S+)$", block, re.MULTILINE)
        assert runbook and (Path(__file__).resolve().parents[1] / runbook.group(1)).is_file(), name


def test_rules_carry_no_secrets_or_content_labels() -> None:
    text = _rules_text().lower()

    for forbidden in ("password", "token", "api_key", "question", "tenant_id", "request_id"):
        assert forbidden not in text, forbidden
