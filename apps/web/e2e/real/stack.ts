/**
 * Local real-stack harness (no Docker): the production API code with a SCRIPTED provider, plus the
 * production web bundle served by `vite preview`, which proxies /api to the API.
 *
 * Tests run in separate worker processes, so process state lives in pid files under work/e2e-stack
 * (gitignored). `restartApi` kills and restarts only the API process, keeping its data directory,
 * which is how backend-restart recovery is exercised.
 */
import { spawn, execFileSync } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

export const API_PORT = 8010;
export const WEB_PORT = 4180;
export const WEB_URL = `http://127.0.0.1:${WEB_PORT}`;

const webRoot = resolve(import.meta.dirname, "../..");
const repoRoot = resolve(webRoot, "../..");
const stateDir = resolve(repoRoot, "work/e2e-stack");
const dataDir = resolve(stateDir, "data");
const apiPidFile = resolve(stateDir, "api.pid");
const webPidFile = resolve(stateDir, "web.pid");

function python(): string {
  const unix = resolve(repoRoot, ".venv/bin/python");
  const windows = resolve(repoRoot, ".venv/Scripts/python.exe");
  if (existsSync(windows)) return windows;
  if (existsSync(unix)) return unix;
  throw new Error("Create the virtual environment first (see AGENTS.md development commands).");
}

function startProcess(
  command: string,
  args: string[],
  pidFile: string,
  logName: string,
  env: NodeJS.ProcessEnv,
  cwd: string,
): void {
  const log = openSync(resolve(stateDir, logName), "a");
  const child = spawn(command, args, {
    cwd,
    env: { ...process.env, ...env },
    stdio: ["ignore", log, log],
    windowsHide: true,
    detached: process.platform !== "win32",
  });
  closeSync(log);
  if (child.pid === undefined) throw new Error(`Could not start ${logName}`);
  writeFileSync(pidFile, String(child.pid));
  child.unref();
}

function killFromPidFile(pidFile: string): void {
  if (!existsSync(pidFile)) return;
  const pid = Number(readFileSync(pidFile, "utf-8"));
  rmSync(pidFile, { force: true });
  if (!Number.isInteger(pid) || pid <= 0) return;
  try {
    if (process.platform === "win32") {
      execFileSync("taskkill", ["/pid", String(pid), "/T", "/F"], { stdio: "ignore" });
    } else {
      process.kill(-pid, "SIGKILL");
    }
  } catch {
    // Already gone.
  }
}

async function isReachable(url: string): Promise<boolean> {
  try {
    return (await fetch(url, { signal: AbortSignal.timeout(2_000) })).ok;
  } catch {
    return false;
  }
}

async function waitFor(url: string, up: boolean, timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if ((await isReachable(url)) === up) return;
    await new Promise((done) => setTimeout(done, 250));
  }
  throw new Error(`${url} did not become ${up ? "ready" : "unavailable"} within ${timeoutMs} ms`);
}

function startApi(): void {
  startProcess(
    python(),
    [
      "-m", "uvicorn", "--factory", "tests.e2e.retail_app:build_app",
      "--host", "127.0.0.1", "--port", String(API_PORT), "--no-server-header",
    ],
    apiPidFile,
    "api.log",
    { E2E_DATA_DIR: dataDir, PYTHONPATH: repoRoot, PYTHONUNBUFFERED: "1" },
    repoRoot,
  );
}

export async function startStack(): Promise<void> {
  killFromPidFile(apiPidFile);
  killFromPidFile(webPidFile);
  rmSync(stateDir, { recursive: true, force: true });
  mkdirSync(stateDir, { recursive: true });

  startApi();
  startProcess(
    process.execPath,
    [resolve(webRoot, "node_modules/vite/bin/vite.js"), "preview", "--host", "127.0.0.1",
      "--port", String(WEB_PORT), "--strictPort"],
    webPidFile,
    "web.log",
    { E2E_API_PORT: String(API_PORT) },
    webRoot,
  );
  await waitFor(`http://127.0.0.1:${API_PORT}/health/ready`, true);
  await waitFor(`${WEB_URL}/`, true);
}

/** Stops only the API process and starts a new one on the same data directory. */
export async function restartApi(): Promise<void> {
  killFromPidFile(apiPidFile);
  await waitFor(`http://127.0.0.1:${API_PORT}/health/ready`, false);
  startApi();
  await waitFor(`http://127.0.0.1:${API_PORT}/health/ready`, true);
}

export function stopStack(): void {
  killFromPidFile(webPidFile);
  killFromPidFile(apiPidFile);
}
