import createPlotlyComponent from "react-plotly.js/factory";
import Plotly from "plotly.js-cartesian-dist-min";

// Plotly's default entry bundles every trace type (about 4.5 MB minified). The API only produces
// line, bar, histogram, and scatter figures (src/charts/chart_builder.py, guarded by
// tests/test_chart_trace_types.py), all of which are in the cartesian partial bundle. Adding a
// non-cartesian trace type on the server requires switching to a bundle that includes it.
// See docs/architecture/frontend-bundle-budget.md.
export default createPlotlyComponent(Plotly);
