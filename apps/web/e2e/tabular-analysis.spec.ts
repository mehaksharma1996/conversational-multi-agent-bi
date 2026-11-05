import {
  analysisFixture,
  consentCurrentFixture,
  consentMissingFixture,
  conversationFixture,
  documentCollectionFixture,
  messageFixture,
  readyDatasetFixture,
  reviewDatasetFixture,
  uploadFixture,
  workspaceFixture,
} from "./fixtures";
import { expect, test } from "./test";

test("analyst reviews the schema before running deterministic analysis", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/config") {
      await route.fallback();
      return;
    }

    if (request.method() === "GET" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 200, json: { items: [] } });
      return;
    }
    if (request.method() === "GET" && path === "/api/v1/tabular-capabilities") {
      await route.fulfill({
        status: 200,
        json: {
          supported_extensions: [".csv", ".xls", ".xlsx"],
          max_upload_bytes: 50 * 1024 * 1024,
          max_rows: 1_000_000,
        },
      });
      return;
    }
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
  await page.getByRole("link", { name: /Data/ }).click();
  await page.getByLabel("CSV or Excel file").setInputFiles({
    name: "transactions.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("amount,merchant\n25,North\n100,South\n900,North"),
  });
  await page.getByRole("button", { name: "Upload and profile" }).click();

  await expect(page.getByRole("heading", { name: "Confirm the canonical schema" })).toBeVisible();
  await expect(page.getByLabel("amount")).toHaveValue("amount");
  await page.getByRole("button", { name: "Confirm schema" }).click();

  await expect(page.getByRole("heading", { name: "transactions.csv is ready for analysis" })).toBeFocused();
  await page.getByRole("button", { name: "Open analysis" }).click();
  await expect(page.getByRole("heading", { name: "Configure anomaly review" })).toBeVisible();
  await page.getByRole("button", { name: "Run deterministic analysis" }).click();

  await expect(page.getByRole("heading", { name: "Analysis dashboard" })).toBeVisible();
  await expect(page.getByText("Anomalies flagged")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Business analysis summary" })).toBeVisible();
});

test("data setup validates files, recovers from failure, and explains ambiguous profiling", async ({ page }) => {
  let uploadAttempts = 0;
  const ambiguous = {
    ...reviewDatasetFixture,
    column_warnings: { amount: ["Mixed numeric formats were left as text for review."] },
    schema_mapping: {
      ...reviewDatasetFixture.schema_mapping,
      mappings: {
        ...reviewDatasetFixture.schema_mapping.mappings,
        amount: {
          ...reviewDatasetFixture.schema_mapping.mappings.amount,
          confidence: 0.4,
          reason: "Only numeric-looking column available.",
        },
      },
    },
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/config") return route.fallback();
    if (request.method() === "GET" && path === "/api/v1/workspaces") {
      return route.fulfill({ status: 200, json: { items: [] } });
    }
    if (request.method() === "POST" && path === "/api/v1/workspaces") {
      return route.fulfill({ status: 201, json: workspaceFixture });
    }
    if (request.method() === "GET" && path === "/api/v1/tabular-capabilities") {
      return route.fulfill({
        status: 200,
        json: {
          supported_extensions: [".csv", ".xlsx"],
          max_upload_bytes: 1_024,
          max_rows: 500,
        },
      });
    }
    if (request.method() === "GET" && path.endsWith("/overview")) {
      return route.fulfill({
        status: 200,
        json: {
          workspace: workspaceFixture,
          dataset_id: null,
          analysis_id: null,
          document_collection_id: null,
          conversation_id: null,
        },
      });
    }
    if (request.method() === "POST" && path.endsWith("/tabular-uploads")) {
      uploadAttempts += 1;
      if (uploadAttempts === 1) {
        return route.fulfill({
          status: 503,
          json: { error: { code: "temporarily_unavailable", message: "Try again.", request_id: "r1", details: [] } },
        });
      }
      return route.fulfill({ status: 201, json: uploadFixture });
    }
    if (request.method() === "GET" && path.endsWith("/sheets")) {
      return route.fulfill({ status: 200, json: { upload_id: uploadFixture.id, sheets: [] } });
    }
    if (request.method() === "POST" && path.endsWith("/dataset")) {
      return route.fulfill({ status: 201, json: ambiguous });
    }
    return route.abort("failed");
  });

  await page.setViewportSize({ width: 375, height: 780 });
  await page.goto("/");
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("link", { name: /Data/ }).click();
  await expect(page.getByText("Up to 500 rows")).toBeVisible();

  await page.getByLabel("CSV or Excel file").setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("not tabular"),
  });
  await expect(page.getByRole("alert")).toContainText("Choose .csv, .xlsx file");
  expect(uploadAttempts).toBe(0);

  await page.getByLabel("CSV or Excel file").setInputFiles({
    name: "transactions.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("amount,merchant\n25,North"),
  });
  await page.getByRole("button", { name: "Upload and profile" }).click();
  const uploadError = page.getByRole("alert").filter({ hasText: "Try again" });
  await expect(uploadError).toBeFocused();
  await expect(uploadError).toContainText("Try again");
  await expect(page.getByText("The server confirmed that no dataset was created.", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Upload and profile" }).click();

  await expect(page.getByText("Low-confidence suggestions are not selected automatically", { exact: false })).toBeVisible();
  await expect(page.getByText("Mixed numeric formats were left as text for review.")).toBeVisible();
  await expect(page.getByLabel("amount")).toHaveValue("");
  expect(await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")).toBe(true);
});

test("analyst indexes a PDF and receives a consented cited answer", async ({ page }) => {
  let consented = false;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/config") {
      await route.fallback();
      return;
    }

    if (request.method() === "GET" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 200, json: { items: [] } });
      return;
    }
    if (request.method() === "POST" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 201, json: workspaceFixture });
      return;
    }
    if (request.method() === "POST" && path.endsWith("/document-collections")) {
      await route.fulfill({ status: 201, json: documentCollectionFixture });
      return;
    }
    if (request.method() === "GET" && path.endsWith("/consent")) {
      await route.fulfill({ status: 200, json: consented ? consentCurrentFixture : consentMissingFixture });
      return;
    }
    if (request.method() === "PUT" && path.endsWith("/consent")) {
      consented = true;
      await route.fulfill({ status: 200, json: consentCurrentFixture });
      return;
    }
    if (request.method() === "POST" && path.endsWith("/conversations")) {
      await route.fulfill({ status: 201, json: conversationFixture });
      return;
    }
    if (request.method() === "POST" && path.endsWith("/messages")) {
      await route.fulfill({ status: 201, json: messageFixture });
      return;
    }
    await route.abort("failed");
  });

  await page.goto("/");
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await page.getByRole("link", { name: /Documents/ }).click();
  await page.getByLabel("PDF documents").setInputFiles({
    name: "policy.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF policy"),
  });
  await page.getByRole("button", { name: "Upload and index PDFs" }).click();

  await expect(page.getByText("Indexed 1 document(s)", { exact: false })).toBeVisible();
  await page.getByRole("link", { name: /Ask/ }).click();
  await page.getByRole("button", { name: "Accept this notice for Gemini" }).click();
  await page.getByLabel("Business question").fill(messageFixture.question);
  await page.getByRole("button", { name: "Ask workbench" }).click();

  await expect(page.getByText(messageFixture.answer)).toBeVisible();
  await expect(page.getByText("rag route", { exact: true })).toBeVisible();
  await page.getByText("Retrieved document sources").click();
  await expect(page.getByText("policy.pdf, page 2")).toBeVisible();
});
