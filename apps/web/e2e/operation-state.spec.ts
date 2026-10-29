import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { readyDatasetFixture, reviewDatasetFixture, uploadFixture, workspaceFixture } from "./fixtures";
import { expect, test } from "./test";

// Mocked API (no backend): replacement confirmation, failure reconciliation, and the dialog's real
// browser behaviour (inert background, focus containment), which jsdom cannot show.

interface Journey {
  uploads: number;
  failSecondUpload: boolean;
}

async function routeJourney(page: Page, journey: Journey) {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path === "/api/v1/auth/config") {
      await route.fallback();
    } else if (method === "GET" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 200, json: { items: [] } });
    } else if (method === "POST" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 201, json: workspaceFixture });
    } else if (method === "GET" && path.endsWith("/overview")) {
      await route.fulfill({
        status: 200,
        json: {
          workspace: workspaceFixture,
          dataset_id: readyDatasetFixture.id,
          analysis_id: null,
          document_collection_id: null,
          conversation_id: null,
        },
      });
    } else if (method === "POST" && path.endsWith("/tabular-uploads")) {
      journey.uploads += 1;
      if (journey.uploads > 1 && journey.failSecondUpload) {
        await route.fulfill({
          status: 503,
          json: { error: { code: "service_unavailable", message: "The service is temporarily unavailable." } },
        });
      } else {
        await route.fulfill({ status: 201, json: uploadFixture });
      }
    } else if (method === "GET" && path.endsWith("/sheets")) {
      await route.fulfill({ status: 200, json: { upload_id: uploadFixture.id, sheets: [] } });
    } else if (method === "POST" && path.endsWith("/dataset")) {
      await route.fulfill({ status: 201, json: reviewDatasetFixture });
    } else if (method === "PUT" && path.endsWith("/schema-mapping")) {
      await route.fulfill({ status: 200, json: readyDatasetFixture });
    } else {
      await route.abort("failed");
    }
  });
}

async function chooseFile(page: Page) {
  await page.getByLabel("CSV or Excel file").setInputFiles({
    name: "transactions.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("amount,merchant\n25,North\n100,South"),
  });
}

async function reachReadyDataset(page: Page) {
  await page.goto("/");
  await page.getByRole("link", { name: /Data/ }).click();
  await chooseFile(page);
  await page.getByRole("button", { name: "Upload and profile" }).click();
  await page.getByRole("button", { name: "Confirm schema" }).click();
  await page.getByRole("link", { name: /Data/ }).click();
  await expect(page.getByRole("heading", { name: /is ready for analysis/ })).toBeVisible();
}

async function focusIsInsideDialog(page: Page) {
  return page.evaluate<boolean>('document.activeElement?.closest("dialog[open]") !== null');
}

test("replacing a dataset asks first in a modal that contains focus and passes axe", async ({ page }) => {
  const journey = { uploads: 0, failSecondUpload: false };
  await routeJourney(page, journey);
  await reachReadyDataset(page);

  await chooseFile(page);
  await page.getByRole("button", { name: "Upload and profile" }).click();
  const dialog = page.getByRole("dialog", { name: /^Replace / });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();

  for (const key of ["Tab", "Tab", "Tab", "Shift+Tab", "Shift+Tab"]) {
    await page.keyboard.press(key);
    const inside = await focusIsInsideDialog(page);
    const onBody = await page.evaluate<boolean>("document.activeElement === document.body");
    expect(inside || onBody).toBe(true);
  }
  // The page behind a modal dialog is inert: it cannot receive a click.
  expect(await page.evaluate<boolean>('document.querySelector("dialog")?.matches(":modal") ?? false')).toBe(true);
  await expect(
    page.getByRole("link", { name: /Overview/ }).click({ trial: true, timeout: 1_000 }),
  ).rejects.toThrow();

  const result = await new AxeBuilder({ page }).analyze();
  expect(result.violations).toEqual([]);

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("button", { name: "Upload and profile" })).toBeFocused();
  expect(journey.uploads).toBe(1);
});

test("a failed replacement keeps the data and says the server confirmed nothing changed", async ({ page }) => {
  const journey = { uploads: 0, failSecondUpload: true };
  await routeJourney(page, journey);
  await reachReadyDataset(page);

  await chooseFile(page);
  await page.getByRole("button", { name: "Upload and profile" }).click();
  await page.getByRole("dialog", { name: /^Replace / }).getByRole("button", { name: "Upload and replace" }).click();

  await expect(page.getByRole("alert")).toContainText("temporarily unavailable");
  await expect(page.getByText(/Nothing was replaced: .* is still in use\./)).toBeVisible();
  await expect(page.getByRole("heading", { name: /is ready for analysis/ })).toBeVisible();
  await expect(page.getByTestId("operation-announcer")).toContainText("Data upload failed.");
  const result = await new AxeBuilder({ page }).analyze();
  expect(result.violations).toEqual([]);
});
