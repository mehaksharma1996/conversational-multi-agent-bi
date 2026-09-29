import { expect, test } from "@playwright/test";

import {
  analysisFixture,
  readyDatasetFixture,
  reviewDatasetFixture,
  uploadFixture,
  workspaceFixture,
} from "./fixtures";

test("analyst reviews the schema before running deterministic analysis", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;

    if (request.method() === "POST" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 201, json: workspaceFixture });
      return;
    }
    if (request.method() === "POST" && path.endsWith("/tabular-uploads")) {
      await route.fulfill({ status: 201, json: uploadFixture });
      return;
    }
    if (request.method() === "GET" && path.endsWith("/sheets")) {
      await route.fulfill({ status: 200, json: { upload_id: uploadFixture.id, sheets: [] } });
      return;
    }
    if (request.method() === "POST" && path.endsWith("/dataset")) {
      await route.fulfill({ status: 201, json: reviewDatasetFixture });
      return;
    }
    if (request.method() === "PUT" && path.endsWith("/schema-mapping")) {
      await route.fulfill({ status: 200, json: readyDatasetFixture });
      return;
    }
    if (request.method() === "POST" && path.endsWith("/analyses")) {
      await route.fulfill({ status: 201, json: analysisFixture });
      return;
    }
    await route.abort("failed");
  });

  await page.goto("/");
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await page.getByLabel("CSV or Excel file").setInputFiles({
    name: "transactions.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("amount,merchant\n25,North\n100,South\n900,North"),
  });
  await page.getByRole("button", { name: "Upload and profile" }).click();

  await expect(page.getByRole("heading", { name: "Confirm the canonical schema" })).toBeVisible();
  await expect(page.getByLabel("amount")).toHaveValue("amount");
  await page.getByRole("button", { name: "Confirm schema" }).click();

  await expect(page.getByRole("heading", { name: "Configure anomaly review" })).toBeVisible();
  await page.getByRole("button", { name: "Run deterministic analysis" }).click();

  await expect(page.getByRole("heading", { name: "Analysis dashboard" })).toBeVisible();
  await expect(page.getByText("Anomalies flagged")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Business analysis summary" })).toBeVisible();
});
