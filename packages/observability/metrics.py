"""In-process, content-free metrics derived from allowlisted telemetry (issue #15).

``MetricsRegistry`` is a ``TelemetrySink``. It sees only events that already passed the telemetry
allowlist (``packages/observability/telemetry.py``), and it uses a fixed set of metric families
whose labels come from allowlisted *token* attributes (event names, route templates, outcomes,
error categories, provider and model names, status classes). It never receives a question, SQL,
row, document text, prompt, file name, tenant identifier, or secret, and a hard cap on distinct
label values stops a misbehaving caller from creating unbounded series.

``render()`` produces the Prometheus text exposition format, which most metric stores can scrape or
convert; no client library or vendor SDK is required.
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

from packages.observability.errors import ERROR_CATEGORIES
from packages.observability.telemetry import TelemetryEvent, TelemetrySink

OVERFLOW = "other"
DURATION_BUCKETS_MS: tuple[float, ...] = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)
_OUTCOMES = {"success", "failure"}
_ROUTES = {"memory", "sql", "rag", "hybrid", "unsupported"}
_ERROR_CATEGORIES = set(ERROR_CATEGORIES)

Labels = tuple[tuple[str, str], ...]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: Labels, extra: Labels = ()) -> str:
    pairs = [*labels, *extra]
    if not pairs:
        return ""
    return "{" + ",".join(f'{name}="{_escape(value)}"' for name, value in pairs) + "}"


class FanOutTelemetrySink:
    """Deliver each event to every sink; one failing sink never affects the others."""

    def __init__(self, sinks: list[TelemetrySink]) -> None:
        self._sinks = list(sinks)

    def emit(self, event: TelemetryEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:  # a metrics or log failure must not break the request path
                continue


class MetricsRegistry:
    """Thread-safe counters and histograms with bounded label cardinality."""

    def __init__(self, max_label_values: int = 64) -> None:
        if max_label_values < 1:
            raise ValueError("max_label_values must be at least 1.")
        self._max = max_label_values
        self._lock = Lock()
        self._seen: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._counters: dict[str, dict[Labels, float]] = defaultdict(lambda: defaultdict(float))
        self._histograms: dict[str, dict[Labels, list[float]]] = defaultdict(dict)
        self._help: dict[str, str] = {}

    # --- TelemetrySink -----------------------------------------------------------------------

    def emit(self, event: TelemetryEvent) -> None:
        attributes = event.attributes
        with self._lock:
            outcome = attributes.get("outcome")
            outcome_label = outcome if isinstance(outcome, str) and outcome in _OUTCOMES else "none"
            self._count(
                "bi_events_total",
                "Telemetry events by name and outcome.",
                (
                    ("event", self._label("bi_events_total", "event", event.name)),
                    ("outcome", outcome_label),
                ),
            )
            if event.dropped_attributes:
                self._count(
                    "bi_dropped_attributes_total",
                    "Attributes rejected by the telemetry allowlist (should stay at zero).",
                    (),
                    float(event.dropped_attributes),
                )
            duration = attributes.get("duration_ms")
            if isinstance(duration, int | float) and not isinstance(duration, bool):
                self._observe(
                    "bi_event_duration_ms",
                    "Duration of timed operations in milliseconds.",
                    (("event", self._label("bi_event_duration_ms", "event", event.name)),),
                    float(duration),
                )
            category = attributes.get("error_category")
            if outcome == "failure" and isinstance(category, str):
                self._count(
                    "bi_errors_total",
                    "Failed operations by event and safe error category.",
                    (
                        ("event", self._label("bi_errors_total", "event", event.name)),
                        (
                            "error_category",
                            self._label(
                                "bi_errors_total",
                                "error_category",
                                category if category in _ERROR_CATEGORIES else OVERFLOW,
                            ),
                        ),
                    ),
                )
            self._derived(event)

    def _derived(self, event: TelemetryEvent) -> None:
        attributes = event.attributes
        if event.name == "http.request":
            route = attributes.get("http_route")
            status = attributes.get("status_code")
            if isinstance(route, str) and isinstance(status, int) and not isinstance(status, bool):
                self._count(
                    "bi_http_requests_total",
                    "HTTP requests by route template and status class.",
                    (
                        ("route", self._label("bi_http_requests_total", "route", route)),
                        ("status_class", f"{status // 100}xx"),
                    ),
                )
        if event.name == "agent.answer":
            route = attributes.get("route")
            if isinstance(route, str):
                self._count(
                    "bi_answers_total",
                    "Answered questions by route.",
                    (
                        (
                            "route",
                            self._label(
                                "bi_answers_total", "route", route if route in _ROUTES else OVERFLOW
                            ),
                        ),
                    ),
                )
        if event.name == "llm.call":
            provider = self._label(
                "bi_llm_calls_total", "provider", str(attributes.get("llm_provider", "unknown"))
            )
            model = self._label(
                "bi_llm_calls_total", "model", str(attributes.get("llm_model", "unknown"))
            )
            outcome = "failure" if attributes.get("outcome") == "failure" else "success"
            self._count(
                "bi_llm_calls_total",
                "Model calls by provider, model, and outcome.",
                (("provider", provider), ("model", model), ("outcome", outcome)),
            )
            for name, kind in (("llm_prompt_tokens", "prompt"), ("llm_output_tokens", "output")):
                value = attributes.get(name)
                if isinstance(value, int) and not isinstance(value, bool):
                    self._count(
                        "bi_llm_tokens_total",
                        "Tokens reported by model providers.",
                        (("provider", provider), ("model", model), ("kind", kind)),
                        float(value),
                    )
            retries = attributes.get("llm_retries")
            if isinstance(retries, int) and not isinstance(retries, bool) and retries > 0:
                self._count("bi_llm_retries_total", "Model call retries.", (), float(retries))
        if event.name == "rate_limit.decision" and attributes.get("rate_limited") is True:
            operation = attributes.get("rate_limit_operation")
            if isinstance(operation, str):
                self._count(
                    "bi_rate_limited_total",
                    "Requests refused by a rate limit.",
                    (("operation", self._label("bi_rate_limited_total", "operation", operation)),),
                )
        rejected = attributes.get("retrieval_rejected_distance")
        if isinstance(rejected, int) and not isinstance(rejected, bool) and rejected > 0:
            self._count(
                "bi_retrieval_rejected_total",
                "Retrieval candidates rejected by the distance threshold.",
                (),
                float(rejected),
            )

    # --- internals ---------------------------------------------------------------------------

    def _label(self, metric: str, name: str, value: str) -> str:
        seen = self._seen[(metric, name)]
        if value in seen:
            return value
        if len(seen) >= self._max:
            return OVERFLOW
        seen.add(value)
        return value

    def _count(self, name: str, help_text: str, labels: Labels, amount: float = 1.0) -> None:
        self._help[name] = help_text
        self._counters[name][labels] += amount

    def _observe(self, name: str, help_text: str, labels: Labels, value: float) -> None:
        self._help[name] = help_text
        series = self._histograms[name].setdefault(labels, [0.0] * (len(DURATION_BUCKETS_MS) + 2))
        for index, bound in enumerate(DURATION_BUCKETS_MS):
            if value <= bound:
                series[index] += 1
        series[len(DURATION_BUCKETS_MS)] += 1  # the +Inf bucket (every observation)
        series[len(DURATION_BUCKETS_MS) + 1] += value  # the sum

    # --- exposition --------------------------------------------------------------------------

    def render(self) -> str:
        """Prometheus text format 0.0.4."""
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._counters):
                lines += [f"# HELP {name} {self._help[name]}", f"# TYPE {name} counter"]
                for labels, value in sorted(self._counters[name].items()):
                    lines.append(f"{name}{_format_labels(labels)} {value:g}")
            for name in sorted(self._histograms):
                lines += [f"# HELP {name} {self._help[name]}", f"# TYPE {name} histogram"]
                for labels, series in sorted(self._histograms[name].items()):
                    for index, bound in enumerate(DURATION_BUCKETS_MS):
                        le = (("le", f"{bound:g}"),)
                        lines.append(f"{name}_bucket{_format_labels(labels, le)} {series[index]:g}")
                    inf = (("le", "+Inf"),)
                    total = series[len(DURATION_BUCKETS_MS)]
                    lines.append(f"{name}_bucket{_format_labels(labels, inf)} {total:g}")
                    lines.append(f"{name}_sum{_format_labels(labels)} {series[-1]:g}")
                    lines.append(f"{name}_count{_format_labels(labels)} {total:g}")
        return "\n".join(lines) + "\n"
