// Fails the build when the production bundle exceeds the budgets in bundle-budget.json.
//
//   npm run build && npm run check:bundle
//
// Sizes are measured on the built files in dist/assets: raw bytes and gzip level 9 (what the nginx
// image serves with compression). The entry chunk is the script that dist/index.html loads; the
// chart chunk is the largest JavaScript asset (the lazily loaded Plotly bundle). Budgets and the
// rationale live in docs/architecture/frontend-bundle-budget.md.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const root = fileURLToPath(new URL("..", import.meta.url));
const assets = join(root, "dist", "assets");
const budget = JSON.parse(readFileSync(join(root, "bundle-budget.json"), "utf8"));

const kib = (bytes) => bytes / 1024;
const files = readdirSync(assets)
  .filter((name) => name.endsWith(".js"))
  .map((name) => {
    const data = readFileSync(join(assets, name));
    return { name, raw: data.length, gzip: gzipSync(data, { level: 9 }).length };
  })
  .sort((a, b) => b.raw - a.raw);

if (files.length === 0) {
  console.error("No JavaScript assets found in dist/assets; run `npm run build` first.");
  process.exit(2);
}

const html = readFileSync(join(root, "dist", "index.html"), "utf8");
const entryName = /src="\/assets\/([^"]+\.js)"/.exec(html)?.[1];
const entry = files.find((file) => file.name === entryName);
const chart = files[0];
const totalGzip = files.reduce((sum, file) => sum + file.gzip, 0);

const checks = [
  ["entry chunk gzip", entry ? kib(entry.gzip) : Number.NaN, budget.entryGzipKiB],
  ["chart chunk raw", kib(chart.raw), budget.chartChunkRawKiB],
  ["chart chunk gzip", kib(chart.gzip), budget.chartChunkGzipKiB],
  ["total JS gzip", kib(totalGzip), budget.totalJsGzipKiB],
];

let failed = false;
console.log(`entry: ${entryName ?? "NOT FOUND"}   chart chunk: ${chart.name}`);
for (const [label, actual, limit] of checks) {
  const ok = Number.isFinite(actual) && actual <= limit;
  failed ||= !ok;
  console.log(
    `${ok ? "PASS" : "FAIL"}  ${label.padEnd(18)} ${actual.toFixed(1).padStart(8)} KiB  (budget ${limit} KiB)`,
  );
}
if (failed) {
  console.error(
    "\nBundle budget exceeded. Reduce the bundle, or raise the budget in bundle-budget.json with " +
      "a justification in docs/architecture/frontend-bundle-budget.md.",
  );
  process.exit(1);
}
