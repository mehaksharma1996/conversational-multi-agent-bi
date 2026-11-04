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

import re
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping
from threading import Lock

from packages.observability.errors import ERROR_CATEGORIES
from packages.observability.telemetry import ScopedTelemetrySink, TelemetryEvent, TelemetrySink

OVERFLOW = "other"
DURATION_BUCKETS_MS: tuple[float, ...] = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)
_OUTCOMES = {"success", "failure"}
_ROUTES = {"memory", "sql", "rag", "hybrid", "unsupported"}
_JOB_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled", "expired"}
_ERROR_CATEGORIES = set(ERROR_CATEGORIES)
# OBS-1 (#146): closed sets, so no investigation metric label can hold an identifier. This package
# does not import the investigation contracts; a test keeps these equal to them.
_INVESTIGATION_PHASES = {"planning", "execution"}
INVESTIGATION_STATUSES = frozenset(
    {
        "planning",
        "awaiting_plan_approval",
        "queued",
        "running",
        "awaiting_sql_approval",
        "completed",
        "completed_partial",
        "budget_exhausted",
        "plan_invalid",
        "plan_rejected",
        "cancelled",
        "failed",
        "interrupted",
        "expired",
    }
)
INVESTIGATION_AGENTS = frozenset(
    {
        "supervisor",
        "sql_analyst",
        "document_research",
        "business_investigator",
        "critic",
        "report_composer",
        "report_verifier",
    }
)
INVESTIGATION_TASK_KINDS = frozenset({"sql_analysis", "document_research", "hypothesis_test"})
INVESTIGATION_TASK_STATUSES = frozenset({"succeeded", "failed", "skipped", "refused", "cancelled"})
INVESTIGATION_COST_BOUNDS = frozenset({"enforced_upper_bound", "indicative", "unavailable"})
# Per invocation, never a root's ``ledger_*`` totals: a resumed run would count them twice.
_LEDGER_RESOURCES = {
    "ledger_invocation_model_calls": "model_calls",
    "ledger_invocation_input_tokens": "input_tokens",
    "ledger_invocation_output_tokens": "output_tokens",
    "ledger_invocation_sql_executions": "sql_executions",
    "ledger_invocation_retrievals": "retrievals",
    "ledger_invocation_task_attempts": "task_attempts",
    "ledger_invocation_replan_rounds": "replan_rounds",
    "ledger_invocation_critic_rounds": "critic_rounds",
}


def _closed(value: object, allowed: Collection[str]) -> str:
    return value if isinstance(value, str) and value in allowed else OVERFLOW


def _positive_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


Labels = tuple[tuple[str, str], ...]
GaugeSampler = Callable[[], Mapping[Labels, float]]
_GAUGE_NAME = re.compile(r"bi_[a-z0-9_]+")


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

    def open_scope(self, name: str, request_id: str | None) -> dict[int, object]:
        """Open the scope on every scoped sink; the others receive the event when it closes."""
        handles: dict[int, object] = {}
        for index, sink in enumerate(self._sinks):
            if isinstance(sink, ScopedTelemetrySink):
                try:
                    handles[index] = sink.open_scope(name, request_id)
                except Exception:  # that sink just receives the closing event instead
                    continue
        return handles

    def close_scope(self, scope: object, event: TelemetryEvent) -> None:
        handles: Mapping[int, object] = scope if isinstance(scope, dict) else {}
        for index, sink in enumerate(self._sinks):
            try:
                if index in handles and isinstance(sink, ScopedTelemetrySink):
                    sink.close_scope(handles[index], event)
                else:
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
        self._gauges: dict[str, tuple[str, GaugeSampler]] = {}

    def register_gauge(self, name: str, help_text: str, sample: GaugeSampler) -> None:
        """Expose a point-in-time value that is read at scrape time, not derived from events.

        ``sample`` must return only fixed, code-defined label values (never user content or
        identifiers). A sampler that raises is skipped so ``/metrics`` stays available.
        """
        if not _GAUGE_NAME.fullmatch(name):
            raise ValueError("Gauge names must match bi_[a-z0-9_]+.")
        with self._lock:
            if name in self._gauges or name in self._counters or name in self._histograms:
                raise ValueError(f"Metric {name} is already registered.")
            self._gauges[name] = (help_text, sample)

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
        if event.name == "job.transition":
            operation = attributes.get("job_operation")
            status = attributes.get("job_status")
            if isinstance(operation, str) and status in _JOB_STATUSES:
                self._count(
                    "bi_jobs_total",
                    "Job state transitions by operation and resulting status.",
                    (
                        ("operation", self._label("bi_jobs_total", "operation", operation)),
                        ("status", str(status)),
                    ),
                )
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
        self._investigation(event)

    def _investigation(self, event: TelemetryEvent) -> None:
        """OBS-1 (#146): closed labels only, never a tenant, investigation, or task identifier."""
        attributes = event.attributes
        if event.name == "investigation_record.terminal":
            self._count(
                "bi_investigation_ended_total",
                "Investigations whose record reached a terminal status, once each, whichever path "
                "ended them, including a cancellation while idle, a rejection, an expiry, or a "
                "restart.",
                (
                    (
                        "status",
                        _closed(attributes.get("investigation_status"), INVESTIGATION_STATUSES),
                    ),
                ),
            )
        if event.name == "investigation.run":
            phase = _closed(attributes.get("investigation_phase"), _INVESTIGATION_PHASES)
            status = _closed(attributes.get("investigation_status"), INVESTIGATION_STATUSES)
            self._count(
                "bi_investigation_runs_total",
                "Investigation graph invocations by phase and resulting status.",
                (("phase", phase), ("status", status)),
            )
            if attributes.get("investigation_terminal") is True:
                self._count(
                    "bi_investigation_terminal_total",
                    "Graph invocations that ended the run, by phase and terminal status. An "
                    "end with no invocation is only in bi_investigation_ended_total.",
                    (("phase", phase), ("status", status)),
                )
            for name, resource in _LEDGER_RESOURCES.items():
                amount = _positive_count(attributes.get(name))
                if amount:
                    self._count(
                        "bi_investigation_ledger_charged_total",
                        "Usage the budget ledger charged, an upper-bound estimate where a provider "
                        "reported no usage. Never a provider bill.",
                        (("phase", phase), ("resource", resource)),
                        float(amount),
                    )
            cost = _positive_count(attributes.get("investigation_estimated_cost_microusd"))
            if cost:
                bound = _closed(
                    attributes.get("investigation_estimated_cost_bound"), INVESTIGATION_COST_BOUNDS
                )
                self._count(
                    "bi_investigation_estimated_cost_microusd_total",
                    "ESTIMATE, not spend: the ledger's cost of exhausting its token limits at the "
                    "configured prices, in micro-USD.",
                    (("phase", phase), ("bound", bound)),
                    float(cost),
                )
        if event.name == "investigation.agent":
            agent = _closed(attributes.get("investigation_agent"), INVESTIGATION_AGENTS)
            outcome = _closed(attributes.get("outcome"), {"success", "failure", "paused"})
            self._count(
                "bi_investigation_agent_steps_total",
                "Investigation steps taken by each agent, by outcome.",
                (("agent", agent), ("outcome", outcome)),
            )
            for name, kind in (
                ("ledger_input_tokens", "input"),
                ("ledger_output_tokens", "output"),
            ):
                amount = _positive_count(attributes.get(name))
                if amount:
                    self._count(
                        "bi_investigation_agent_charged_tokens_total",
                        "Tokens the budget ledger charged to each agent's steps, an upper-bound "
                        "estimate where a provider reported no usage. Never a provider bill.",
                        (("agent", agent), ("kind", kind)),
                        float(amount),
                    )
            duration = attributes.get("duration_ms")
            if isinstance(duration, float):
                self._observe(
                    "bi_investigation_agent_duration_ms",
                    "Duration of each agent's investigation steps in milliseconds.",
                    (("agent", agent),),
                    duration,
                )
        if event.name == "investigation.task":
            kind_label = _closed(
                attributes.get("investigation_task_kind"), INVESTIGATION_TASK_KINDS
            )
            self._count(
                "bi_investigation_tasks_total",
                "Investigation tasks that ended, by kind and status.",
                (
                    ("kind", kind_label),
                    (
                        "status",
                        _closed(
                            attributes.get("investigation_task_status"),
                            INVESTIGATION_TASK_STATUSES,
                        ),
                    ),
                ),
            )
            duration = attributes.get("duration_ms")
            if isinstance(duration, float):
                self._observe(
                    "bi_investigation_task_duration_ms",
                    "Active duration of investigation tasks in milliseconds; excludes waiting.",
                    (("kind", kind_label),),
                    duration,
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
        # Samplers run outside the lock: they may take their own locks (the job executor emits
        # events while holding one), and a nested acquire in the other order would deadlock.
        with self._lock:
            gauges = sorted(self._gauges.items())
        sampled: list[tuple[str, str, Mapping[Labels, float]]] = []
        for name, (help_text, sample) in gauges:
            try:
                sampled.append((name, help_text, dict(sample())))
            except Exception:  # a failing probe must not take the whole scrape down
                continue
        for name, help_text, values in sampled:
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
            for labels, value in sorted(values.items()):
                lines.append(f"{name}{_format_labels(labels)} {value:g}")
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
