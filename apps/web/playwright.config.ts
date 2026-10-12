import { defineConfig, devices } from "@playwright/test";

import { WEB_URL } from "./e2e/real/stack";

// E2E_REAL_STACK=scripted runs the journeys in e2e/real against the production API code with a
// scripted provider (no Docker, no live model). Every other mode runs the mocked and Docker specs.
const scripted = process.env.E2E_REAL_STACK === "scripted";

export default defineConfig({
  testDir: "./e2e",
  testMatch: scripted ? "real/**/*.spec.ts" : "*.spec.ts",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: !scripted,
  workers: scripted ? 1 : undefined,
  timeout: scripted ? 90_000 : 30_000,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? (scripted ? WEB_URL : "http://127.0.0.1:4173"),
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
