"""Content-free metrics derived from allowlisted telemetry (issue #15)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from packages.observability import (
    FanOutTelemetrySink,
    InMemoryTelemetrySink,
    MetricsRegistry,
    Telemetry,
    bind_request_id,
)
from packages.observability.metrics import OVERFLOW
from tests.test_api_features import FakeEmbedder, FakeLLM, _settings
from tests.test_utils import isolated_directory_path

SAFE_LABEL_VALUE = re.compile(r"[A-Za-z0-9_.:+/{}\-]+")
SECRET_QUESTION = "What did Jane Doe (SSN 123-45-6789) spend last quarter?"
SECRET_SQL = 'SELECT "ssn" FROM "uploaded_data"'


def _registry() -> tuple[MetricsRegistry, Telemetry]:
    registry = MetricsRegistry()
    return registry, Telemetry(registry)


def _lines(registry: MetricsRegistry) -> list[str]:
    return [line for line in registry.render().splitlines() if not line.startswith("#")]


def test_events_are_counted_by_name_and_outcome_and_timed_in_a_histogram() -> None:
    registry, telemetry = _registry()

    with telemetry.operation("analysis.run", tenant_id="t" * 32) as operation:
        operation.set(row_count=10)
    try:
        with telemetry.operation("analysis.run", tenant_id="t" * 32):
            raise TimeoutError("slow")
    except TimeoutError:
        pass

    lines = _lines(registry)
    assert 'bi_events_total{event="analysis.run",outcome="success"} 1' in lines
    assert 'bi_events_total{event="analysis.run",outcome="failure"} 1' in lines
    assert 'bi_errors_total{error_category="timeout",event="analysis.run"} 1' in lines or (
        'bi_errors_total{event="analysis.run",error_category="timeout"} 1' in lines
    )
    assert 'bi_event_duration_ms_count{event="analysis.run"} 2' in lines
    buckets = [
        float(line.rsplit(" ", 1)[1])
        for line in lines
        if line.startswith('bi_event_duration_ms_bucket{event="analysis.run"')
    ]
    assert buckets == sorted(buckets), "histogram buckets must be cumulative"
    assert buckets[-1] == 2


def test_http_answer_llm_rate_limit_and_retrieval_signals() -> None:
    registry, telemetry = _registry()

    telemetry.emit("http.request", http_route="/api/v1/workspaces", status_code=201, duration_ms=12)
    telemetry.emit("http.request", http_route="/api/v1/workspaces", status_code=503, duration_ms=9)
    telemetry.emit("agent.answer", route="sql", outcome="success", duration_ms=40)
    telemetry.emit(
        "llm.call",
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
        llm_prompt_tokens=120,
        llm_output_tokens=30,
        llm_retries=2,
        outcome="success",
    )
    telemetry.emit("rate_limit.decision", rate_limit_operation="analysis", rate_limited=True)
    telemetry.emit("rate_limit.decision", rate_limit_operation="analysis", rate_limited=False)
    telemetry.emit("retrieval.search", retrieval_rejected_distance=3)

    text = registry.render()
    assert 'bi_http_requests_total{route="/api/v1/workspaces",status_class="2xx"} 1' in text
    assert 'bi_http_requests_total{route="/api/v1/workspaces",status_class="5xx"} 1' in text
    assert 'bi_answers_total{route="sql"} 1' in text
    assert (
        'bi_llm_tokens_total{provider="gemini",model="gemini-2.5-flash",kind="prompt"} 120' in text
    )
    assert 'kind="output"} 30' in text
    assert "bi_llm_retries_total 2" in text
    assert 'bi_rate_limited_total{operation="analysis"} 1' in text
    assert "bi_retrieval_rejected_total 3" in text


def test_content_and_identity_can_never_reach_the_metrics_output() -> None:
    registry, telemetry = _registry()
    with bind_request_id("req-should-not-appear"):
        telemetry.emit(
            "agent.answer",
            tenant_id="tenant-should-not-appear",
            route="sql",
            question=SECRET_QUESTION,
            sql=SECRET_SQL,
            filename="payroll-2026.csv",
            prompt="You are a careful SQLite analyst. SECRET",
            outcome="success",
            error_category="sk-live-abc123def456",  # token-shaped but not a known category
            duration_ms=5,
        )
        telemetry.emit("agent.answer", route="drop-table-users", outcome="failure")

    text = registry.render()
    for forbidden in (
        "Jane",
        "123-45-6789",
        "SELECT",
        "payroll",
        "SECRET",
        "req-should-not-appear",
        "tenant-should-not-appear",
        "sk-live",
    ):
        assert forbidden not in text, forbidden
    # Dropped attributes are visible as a counter so a leak attempt is observable, not silent.
    assert re.search(r"bi_dropped_attributes_total (\d+)", text)
    assert 'route="drop-table-users"' not in text and f'route="{OVERFLOW}"' in text


def test_every_label_value_is_a_short_safe_token() -> None:
    registry, telemetry = _registry()
    telemetry.emit("http.request", http_route="/api/v1/datasets/{dataset_id}", status_code=200)
    telemetry.emit("llm.call", llm_provider="ollama", llm_model="llama3.2:3b", outcome="failure")

    for line in _lines(registry):
        for _, value in re.findall(r'(\w+)="([^"]*)"', line):
            assert SAFE_LABEL_VALUE.fullmatch(value) or value == "+Inf", value
            assert len(value) <= 128


def test_distinct_label_values_are_capped_to_stop_unbounded_series() -> None:
    registry = MetricsRegistry(max_label_values=5)
    telemetry = Telemetry(registry)

    for index in range(200):
        telemetry.emit(f"custom.event_{index}", outcome="success")

    names = {
        match.group(1)
        for match in re.finditer(r'bi_events_total\{event="([^"]+)"', registry.render())
    }
    assert len(names) <= 6 and OVERFLOW in names
    assert 'bi_events_total{event="other",outcome="success"} 195' in registry.render()


def test_a_failing_sink_never_breaks_the_others_or_the_caller() -> None:
    class Broken:
        def emit(self, event: object) -> None:
            raise RuntimeError("down")

    memory = InMemoryTelemetrySink()
    registry = MetricsRegistry()
    telemetry = Telemetry(FanOutTelemetrySink([Broken(), memory, registry]))

    telemetry.emit("agent.answer", route="rag", outcome="success")

    assert [e.name for e in memory.events] == ["agent.answer"]
    assert 'bi_answers_total{route="rag"} 1' in registry.render()


def _app(name: str, **overrides: Any) -> TestClient:
    from apps.api.main import create_app

    settings = replace(_settings(isolated_directory_path(name)), **overrides)
    return TestClient(
        create_app(
            settings=settings,
            embedder_factory=lambda _model: FakeEmbedder(),
            llm_client_factory=lambda _settings: FakeLLM(),
            telemetry_sink=InMemoryTelemetrySink(),
        )
    )


def test_the_metrics_endpoint_is_off_by_default() -> None:
    client = _app("metrics_off")

    assert client.get("/metrics").status_code == 404


def test_the_enabled_endpoint_serves_prometheus_text_without_content_or_identity() -> None:
    client = _app("metrics_on", metrics_enabled=True)
    workspace = client.post("/api/v1/workspaces").json()
    client.get(f"/api/v1/workspaces/{workspace['id']}")
    client.get("/api/v1/workspaces/ws_does_not_exist")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert response.headers["cache-control"] == "no-store"
    text = response.text
    assert 'bi_http_requests_total{route="/api/v1/workspaces",status_class="2xx"} 1' in text
    assert 'route="/api/v1/workspaces/{workspace_id}",status_class="4xx"' in text
    assert workspace["id"] not in text, "resource identifiers must never be labels"
    assert "metrics" not in text.replace("# HELP", ""), "the endpoint does not count itself"


def test_the_metrics_endpoint_is_not_part_of_the_public_api_contract() -> None:
    client = _app("metrics_contract", metrics_enabled=True)

    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


def test_job_transitions_become_content_free_telemetry_and_metrics() -> None:
    from apps.api.main import create_app

    sink = InMemoryTelemetrySink()
    settings = replace(_settings(isolated_directory_path("metrics_jobs")), metrics_enabled=True)
    app = create_app(
        settings=settings,
        embedder_factory=lambda _model: FakeEmbedder(),
        llm_client_factory=lambda _settings: FakeLLM(),
        telemetry_sink=sink,
    )
    tenant = "a" * 32

    def fails(_context: object) -> None:
        raise ValueError(SECRET_QUESTION)

    with TestClient(app) as client:
        executor = app.state.job_executor
        ok, _ = executor.submit(
            tenant_id=tenant,
            workspace_id="ws_" + "1" * 32,
            operation="test.run",
            work=lambda _context: {"private": SECRET_SQL},
            request_id="req-job-1",
        )
        bad, _ = executor.submit(
            tenant_id=tenant,
            workspace_id="ws_" + "1" * 32,
            operation="test.run",
            work=fails,
            max_attempts=1,
        )
        executor.wait(ok.id, tenant, timeout=10)
        executor.wait(bad.id, tenant, timeout=10)
        text = client.get("/metrics").text

    assert 'bi_jobs_total{operation="test.run",status="queued"} 2' in text
    assert 'bi_jobs_total{operation="test.run",status="running"} 2' in text
    assert 'bi_jobs_total{operation="test.run",status="succeeded"} 1' in text
    assert 'bi_jobs_total{operation="test.run",status="failed"} 1' in text
    assert 'bi_event_duration_ms_count{event="job.transition"} 2' in text
    assert 'event="job.transition"' in text and "error_category=" in text
    events = [e for e in sink.events if e.name == "job.transition"]
    assert {e.request_id for e in events if e.attributes["job_status"] == "succeeded"} == {
        "req-job-1"
    }, "the originating request ID correlates the job's telemetry"
    for forbidden in (SECRET_QUESTION, SECRET_SQL, "req-job-1", ok.id, bad.id, tenant):
        assert forbidden not in text, forbidden
    for event in events:
        assert SECRET_QUESTION not in json.dumps(event.as_dict(), default=str)
        assert event.attributes["job_operation"] == "test.run"


def test_gauges_are_sampled_at_scrape_time_and_a_failing_probe_is_skipped() -> None:
    registry = MetricsRegistry()
    value = {"now": 1.0}
    registry.register_gauge("bi_demo_level", "Demo level.", lambda: {(): value["now"]})
    registry.register_gauge("bi_demo_split", "Demo split.", lambda: {(("state", "a"),): 2.0})

    def broken() -> dict[tuple[tuple[str, str], ...], float]:
        raise OSError("probe failed with /private/path")

    registry.register_gauge("bi_demo_broken", "Broken probe.", broken)

    first = registry.render()
    value["now"] = 7.0
    second = registry.render()

    assert "# TYPE bi_demo_level gauge" in first and "bi_demo_level 1" in first
    assert "bi_demo_level 7" in second
    assert 'bi_demo_split{state="a"} 2' in second
    assert "bi_demo_broken" not in second and "/private/path" not in second


def test_gauge_registration_rejects_unsafe_or_duplicate_names() -> None:
    registry = MetricsRegistry()
    registry.register_gauge("bi_ok", "Fine.", lambda: {(): 0.0})

    for bad in ("ok", "bi_Upper", 'bi_x{y="z"}', "bi_ok"):
        try:
            registry.register_gauge(bad, "Nope.", lambda: {(): 0.0})
        except ValueError:
            continue
        raise AssertionError(f"{bad} should have been rejected")


def test_render_does_not_deadlock_when_a_gauge_reads_state_guarded_by_another_lock() -> None:
    import threading

    registry = MetricsRegistry()
    telemetry = Telemetry(registry)
    guard = threading.Lock()

    def sample() -> dict[tuple[tuple[str, str], ...], float]:
        with guard:
            return {(): 1.0}

    registry.register_gauge("bi_locked", "Needs another lock.", sample)

    def emit_while_holding_the_other_lock() -> None:
        with guard:
            telemetry.emit("agent.answer", route="sql", outcome="success")

    worker = threading.Thread(target=emit_while_holding_the_other_lock)
    worker.start()
    for _ in range(50):
        registry.render()
    worker.join(timeout=30)
    assert not worker.is_alive()


def test_enabled_endpoint_exposes_job_and_data_volume_gauges_with_fixed_labels() -> None:
    client = _app("metrics_gauges", metrics_enabled=True)

    text = client.get("/metrics").text

    assert "# TYPE bi_jobs_queued gauge" in text and "bi_jobs_queued 0" in text
    assert "bi_jobs_running 0" in text
    assert "bi_jobs_capacity 10" in text
    for state in ("free", "total", "used"):
        assert f'bi_data_volume_bytes{{state="{state}"}}' in text
    assert "data" not in re.sub(r"bi_data_volume_bytes|application data directory", "", text)


def test_gauges_are_absent_when_metrics_are_disabled() -> None:
    client = _app("metrics_gauges_off")

    assert client.get("/metrics").status_code == 404
