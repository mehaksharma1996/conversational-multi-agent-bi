// Builds the production bundle, then runs the real-stack journeys against the production API code
// with a SCRIPTED provider (no Docker, no live model). See e2e/real/retail-journey.spec.ts.
import { spawnSync } from "node:child_process";

const env = { ...process.env, E2E_REAL_STACK: "scripted" };
const npx = process.platform === "win32" ? "npx.cmd" : "npx";

function run(command, args) {
  const result = spawnSync(command, args, { stdio: "inherit", env, shell: process.platform === "win32" });
  if (result.status !== 0) process.exit(result.status ?? 1);
}

run(npx, ["vite", "build"]);
run(npx, ["playwright", "test", ...process.argv.slice(2)]);
