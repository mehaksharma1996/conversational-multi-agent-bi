import path from "node:path";

import { expect, test } from "./test";

const stackMode = process.env.E2E_REAL_STACK;
const sampleData = path.resolve(import.meta.dirname, "../../../sample_data/transactions.csv");
const samplePolicy = path.resolve(import.meta.dirname, "../../../sample_data/review_policy.pdf");

async function uploadConfirmAndAnalyze(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await page.getByLabel("CSV or Excel file").setInputFiles(sampleData);
  await page.getByRole("button", { name: "Upload and profile" }).click();
  await expect(page.getByRole("heading", { name: "Confirm the canonical schema" })).toBeVisible();
  await page.getByRole("button", { name: "Confirm schema" }).click();
  await expect(page.getByRole("heading", { name: "Configure anomaly review" })).toBeVisible();
  await page.getByLabel("Expected review proportion", { exact: false }).fill("0.1");
  await page.getByRole("button", { name: "Run deterministic analysis" }).click();
  await expect(page.getByRole("heading", { name: "Analysis dashboard" })).toBeVisible();
  await expect(page.getByText("Anomalies flagged")).toBeVisible();
}

test("real local-only stack: tabular analysis, memory, reports, and reset", async ({ page }) => {
  test.skip(stackMode !== "local", "requires the default LOCAL_ONLY_MODE Compose stack");
  await uploadConfirmAndAnalyze(page);
  await page.getByLabel("Business question").fill("What analysis was possible?");
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.getByText("memory route")).toBeVisible();

  const markdownDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download Markdown report" }).click();
  expect((await markdownDownload).suggestedFilename()).toBe("business-intelligence-report.md");
  const pdfDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download PDF report" }).click();
  expect((await pdfDownload).suggestedFilename()).toBe("business-intelligence-report.pdf");

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Reset workspace" }).click();
  await expect(page.getByRole("heading", { name: "Confirm the canonical schema" })).toHaveCount(0);
  await expect(page.getByText("Local workspace ready")).toBeVisible();
});

test("real fake-provider stack: PDF citations, hybrid export, reports, and reset", async ({ page }) => {
  test.skip(stackMode !== "fake", "requires the isolated deterministic-provider overlay");
  await uploadConfirmAndAnalyze(page);
  await page.getByLabel("PDF documents").setInputFiles(samplePolicy);
  await page.getByRole("button", { name: "Upload and index PDFs" }).click();
  await expect(page.getByText("Indexed 1 document(s)", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "I understand, enable Gemini" }).click();

  await page.getByLabel("Business question").fill("What does the policy require?");
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.getByText("rag route")).toBeVisible();
  await page.getByText("Retrieved document sources").click();
  await expect(page.getByText("review_policy.pdf", { exact: false })).toBeVisible();

  await page.getByLabel("Business question").fill("Which uploaded transactions violate the policy?");
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.getByText("hybrid route")).toBeVisible();
  await expect(page.getByText("SELECT", { exact: false })).toBeVisible();

  const csvDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download CSV" }).click();
  expect((await csvDownload).suggestedFilename()).toMatch(/\.csv$/);
  const excelDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download Excel" }).click();
  expect((await excelDownload).suggestedFilename()).toMatch(/\.xlsx$/);
  const reportDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download Markdown report" }).click();
  expect((await reportDownload).suggestedFilename()).toBe("business-intelligence-report.md");

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Reset workspace" }).click();
  await expect(page.getByText("Local workspace ready")).toBeVisible();
});
