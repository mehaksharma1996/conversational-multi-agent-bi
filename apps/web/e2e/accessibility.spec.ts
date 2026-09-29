import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import {
  analysisFixture,
  conversationFixture,
  documentCollectionFixture,
  messageFixture,
  readyDatasetFixture,
  reviewDatasetFixture,
  uploadFixture,
  workspaceFixture,
} from "./fixtures";
import { expect, test } from "./test";

async function expectNoAxeViolations(page: Page, excludePlotly = false) {
  let builder = new AxeBuilder({ page });
  if (excludePlotly) builder = builder.exclude(".js-plotly-plot");
  const result = await builder.analyze();
  expect(result.violations).toEqual([]);
}

async function routeWorkspace(page: Page, workspace = workspaceFixture) {
  await page.route("**/api/v1/workspaces", (route) =>
    route.fulfill({ status: 201, json: workspace }),
  );
}

async function routeTabularJourney(page: Page, includeChart = false) {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 201, json: workspaceFixture });
    } else if (request.method() === "POST" && path.endsWith("/tabular-uploads")) {
      await route.fulfill({ status: 201, json: uploadFixture });
    } else if (request.method() === "GET" && path.endsWith("/sheets")) {
      await route.fulfill({ status: 200, json: { upload_id: uploadFixture.id, sheets: [] } });
    } else if (request.method() === "POST" && path.endsWith("/dataset")) {
      await route.fulfill({ status: 201, json: reviewDatasetFixture });
    } else if (request.method() === "PUT" && path.endsWith("/schema-mapping")) {
      await route.fulfill({ status: 200, json: readyDatasetFixture });
    } else if (request.method() === "POST" && path.endsWith("/analyses")) {
      const chart = {
        title: "Amount trend",
        chart_type: "line",
        description: "Transaction amount over time.",
        figure: {
          data: [{ x: ["2026-01-01"], y: [1025], type: "scatter", mode: "lines" }],
          layout: { title: "Amount trend" },
        },
      };
      await route.fulfill({
        status: 201,
        json: { ...analysisFixture, charts: includeChart ? [chart] : [] },
      });
    } else {
      await route.abort("failed");
    }
  });
}

async function reachSchemaReview(page: Page) {
  await page.goto("/");
  await page.getByLabel("CSV or Excel file").setInputFiles({
    name: "transactions.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("amount,merchant\n25,North\n100,South\n900,North"),
  });
  await page.getByRole("button", { name: "Upload and profile" }).click();
  await expect(page.getByRole("heading", { name: "Confirm the canonical schema" })).toBeVisible();
}

test("axe baseline: workspace ready", async ({ page }) => {
  await routeWorkspace(page);
  await page.goto("/");
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await expectNoAxeViolations(page);
});

test("axe baseline: schema review", async ({ page }) => {
  await routeTabularJourney(page);
  await reachSchemaReview(page);
  await expectNoAxeViolations(page);
});

test("axe baseline: dashboard with charts", async ({ page }) => {
  await routeTabularJourney(page, true);
  await reachSchemaReview(page);
  await page.getByRole("button", { name: "Confirm schema" }).click();
  await page.getByRole("button", { name: "Run deterministic analysis" }).click();
  await expect(page.getByRole("heading", { name: "Amount trend" })).toBeVisible();
  await expectNoAxeViolations(page, true);
});

test("axe baseline: chat answer and provenance warning", async ({ page }) => {
  const warningMessage = {
    ...messageFixture,
    provenance: {
      ...messageFixture.provenance,
      grounding_status: "warnings",
      invalid_citation_count: 1,
    },
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 201, json: { ...workspaceFixture, consent_accepted: true } });
    } else if (request.method() === "POST" && path.endsWith("/document-collections")) {
      await route.fulfill({ status: 201, json: documentCollectionFixture });
    } else if (request.method() === "POST" && path.endsWith("/conversations")) {
      await route.fulfill({ status: 201, json: conversationFixture });
    } else if (request.method() === "POST" && path.endsWith("/messages")) {
      await route.fulfill({ status: 201, json: warningMessage });
    } else {
      await route.abort("failed");
    }
  });
  await page.goto("/");
  await page.getByLabel("PDF documents").setInputFiles({
    name: "policy.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF policy"),
  });
  await page.getByRole("button", { name: "Upload and index PDFs" }).click();
  await page.getByLabel("Business question").fill(messageFixture.question);
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.getByText("Grounding warning:", { exact: false })).toBeVisible();
  await expectNoAxeViolations(page);
});

test("axe baseline: error banner", async ({ page }) => {
  await page.route("**/api/v1/workspaces", (route) =>
    route.fulfill({
      status: 503,
      json: {
        error: {
          code: "service_not_ready",
          message: "The API is not ready to accept work.",
          request_id: "req-accessibility",
          details: [],
        },
      },
    }),
  );
  await page.goto("/");
  await expect(page.getByRole("alert")).toBeVisible();
  await expectNoAxeViolations(page);
});

test("keyboard-only journey reaches content in order with visible focus and no trap", async ({ page }) => {
  await routeWorkspace(page);
  await page.goto("/");

  await page.keyboard.press("Tab");
  const skipLink = page.getByRole("link", { name: "Skip to main content" });
  await expect(skipLink).toBeFocused();
  await expect(skipLink).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();

  await page.keyboard.press("Tab");
  const fileInput = page.getByLabel("CSV or Excel file");
  await expect(fileInput).toBeFocused();
  await expect(fileInput).toHaveCSS("outline-width", "3px");

  const focused: string[] = [];
  for (let index = 0; index < 12; index += 1) {
    await page.keyboard.press("Tab");
    const active = page.locator(":focus");
    focused.push((await active.count()) > 0 ? await active.ariaSnapshot() : "document");
  }
  expect(new Set(focused).size).toBeGreaterThan(2);
  expect(focused.some((snapshot) => snapshot.includes("button"))).toBe(true);
});
