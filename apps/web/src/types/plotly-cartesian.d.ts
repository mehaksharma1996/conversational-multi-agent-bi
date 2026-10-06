// The partial bundle exposes the same runtime API as the full plotly.js package; reuse its types.
declare module "plotly.js-cartesian-dist-min" {
  import Plotly from "plotly.js";
  export default Plotly;
}
