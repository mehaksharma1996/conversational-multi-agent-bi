"""Guards the web client's partial Plotly bundle (apps/web/src/components/PlotlyChart.tsx).

The browser loads ``plotly.js-cartesian-dist-min`` instead of the full 4.5 MB library. A figure that
uses a trace type outside that bundle would render blank, so the server may only build figures from
the constructors below. To add another type, switch the web bundle first
(docs/architecture/frontend-bundle-budget.md).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHART_BUILDER = ROOT / "src" / "charts" / "chart_builder.py"

# Plotly Express constructors and graph-object classes that produce only cartesian traces
# (scatter/line, bar, histogram) and are therefore present in plotly.js-cartesian-dist-min.
ALLOWED_EXPRESS = {"line", "bar", "histogram", "scatter"}
ALLOWED_GRAPH_OBJECTS = {"Scatter", "Bar", "Histogram"}


def _plotly_calls() -> tuple[set[str], set[str]]:
    tree = ast.parse(CHART_BUILDER.read_text(encoding="utf-8"))
    express: set[str] = set()
    graph_objects: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "px":
            express.add(node.func.attr)
        if isinstance(owner, ast.Name) and owner.id == "go":
            graph_objects.add(node.func.attr)
    return express, graph_objects


def test_chart_builder_only_uses_trace_types_in_the_web_bundle() -> None:
    express, graph_objects = _plotly_calls()

    assert express, "expected the chart builder to use Plotly Express"
    assert express <= ALLOWED_EXPRESS, sorted(express - ALLOWED_EXPRESS)
    assert graph_objects <= ALLOWED_GRAPH_OBJECTS, sorted(graph_objects - ALLOWED_GRAPH_OBJECTS)


def test_the_web_client_loads_the_partial_bundle_not_the_full_library() -> None:
    component = (ROOT / "apps" / "web" / "src" / "components" / "PlotlyChart.tsx").read_text(
        encoding="utf-8"
    )
    dashboard = (ROOT / "apps" / "web" / "src" / "components" / "AnalysisDashboard.tsx").read_text(
        encoding="utf-8"
    )

    assert "plotly.js-cartesian-dist-min" in component
    assert 'import("react-plotly.js")' not in dashboard, "the default entry bundles every trace"
    assert 'import("./PlotlyChart")' in dashboard
