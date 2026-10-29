import { randomUUID } from "node:crypto";
import path from "node:path";

import { expect, test } from "./test";

const stackMode = process.env.E2E_REAL_STACK;
const sampleData = path.resolve(import.meta.dirname, "../../../sample_data/transactions.csv");
const samplePolicy = path.resolve(import.meta.dirname, "../../../tests/e2e/review_policy.pdf");
const workspaceHint = "conversational-bi.workspace-hint";

interface WorkspacePayload {
  id: string;
  gemini_configured: boolean;
  local_only_mode: boolean;
  consent_required: boolean;
  consent_accepted: boolean;
}

interface WorkspaceOverviewPayload {
  workspace: WorkspacePayload;
  dataset_id: string | null;
  document_collection_id: string | null;
}

interface ErrorPayload {
  error: { code: string };
}

async function openIsolatedWorkspace(
  page: import("@playwright/test").Page,
  consentRequired: boolean,
): Promise<WorkspacePayload> {
  const created = await page.request.post("/api/v1/workspaces", {
    headers: { "Idempotency-Key": `playwright-${randomUUID()}` },
  });
  expect(created.status()).toBe(201);
  const workspace = (await created.json()) as WorkspacePayload;
  expect(workspace.consent_required).toBe(consentRequired);
  expect(workspace.consent_accepted).toBe(false);

  await page.addInitScript(
    `sessionStorage.setItem(${JSON.stringify(workspaceHint)}, ${JSON.stringify(workspace.id)})`,
  );
  const requestedOverviewIds: string[] = [];
  page.on("request", (request) => {
    const match = new URL(request.url()).pathname.match(
      /^\/api\/v1\/workspaces\/(ws_[0-9a-f]+)\/overview$/,
    );
    if (match?.[1]) requestedOverviewIds.push(match[1]);
  });

  await page.goto("/");
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  expect(requestedOverviewIds).toEqual([workspace.id]);
  const storedHint = await page.evaluate<string>(
    `sessionStorage.getItem(${JSON.stringify(workspaceHint)})`,
  );
  expect(storedHint).toBe(workspace.id);
  return workspace;
}

async function uploadConfirmAndAnalyze(
  page: import("@playwright/test").Page,
  consentRequired: boolean,
): Promise<WorkspacePayload> {
  const workspace = await openIsolatedWorkspace(page, consentRequired);
  await page.getByRole("link", { name: /Data/ }).click();
  await page.getByLabel("CSV or Excel file").setInputFiles(sampleData);
  await page.getByRole("button", { name: "Upload and profile" }).click();
  await expect(page.getByRole("heading", { name: "Confirm the canonical schema" })).toBeVisible();
  await page.getByRole("button", { name: "Confirm schema" }).click();
  await expect(page.getByRole("heading", { name: "Configure anomaly review" })).toBeVisible();
  await page.getByLabel("Expected review proportion", { exact: false }).fill("0.1");
  await page.getByRole("button", { name: "Run deterministic analysis" }).click();
  await expect(page.getByRole("heading", { name: "Analysis dashboard" })).toBeVisible();
  await expect(page.getByText("Anomalies flagged")).toBeVisible();
  return workspace;
}

test("real local-only stack: tabular analysis, memory, reports, and reset", async ({ page }) => {
  test.skip(stackMode !== "local", "requires the default LOCAL_ONLY_MODE Compose stack");
  const workspace = await uploadConfirmAndAnalyze(page, false);
  expect(workspace.local_only_mode).toBe(true);
  expect(workspace.gemini_configured).toBe(false);
  await page.getByRole("link", { name: /Ask/ }).click();
  await page.getByLabel("Business question").fill("What analysis was possible?");
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.getByText("memory route", { exact: true })).toBeVisible();

  await page.getByRole("link", { name: /Analysis/ }).click();
  const markdownDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download Markdown report" }).click();
  expect((await markdownDownload).suggestedFilename()).toBe("business-intelligence-report.md");
  const pdfDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download PDF report" }).click();
  expect((await pdfDownload).suggestedFilename()).toBe("business-intelligence-report.pdf");

  await page.getByRole("button", { name: "Reset workspace" }).click();
  await page.getByRole("dialog", { name: "Delete this workspace?" }).getByRole("button", { name: "Delete workspace" }).click();
  await expect(page.getByRole("heading", { name: "Confirm the canonical schema" })).toHaveCount(0);
  await expect(page.getByText("Local workspace ready")).toBeVisible();
});

test("real fake-provider stack: PDF citations, hybrid export, reports, and reset", async ({ page }) => {
  test.skip(stackMode !== "fake", "requires the isolated deterministic-provider overlay");
  const workspace = await uploadConfirmAndAnalyze(page, true);
  expect(workspace.local_only_mode).toBe(false);
  expect(workspace.gemini_configured).toBe(true);
  await page.getByRole("link", { name: /Documents/ }).click();
  await page.getByLabel("PDF documents").setInputFiles(samplePolicy);
  await page.getByRole("button", { name: "Upload and index PDFs" }).click();
  await expect(page.getByText("Indexed 1 document(s)", { exact: false })).toBeVisible();

  const overviewResponse = await page.request.get(
    `/api/v1/workspaces/${workspace.id}/overview`,
  );
  expect(overviewResponse.status()).toBe(200);
  const overview = (await overviewResponse.json()) as WorkspaceOverviewPayload;
  expect(overview.workspace.id).toBe(workspace.id);
  expect(overview.workspace.consent_required).toBe(true);
  expect(overview.workspace.consent_accepted).toBe(false);
  expect(overview.dataset_id).not.toBeNull();
  expect(overview.document_collection_id).not.toBeNull();

  const conversationResponse = await page.request.post(
    `/api/v1/workspaces/${workspace.id}/conversations`,
    {
      data: {
        dataset_id: overview.dataset_id,
        document_collection_id: overview.document_collection_id,
      },
    },
  );
  expect(conversationResponse.status()).toBe(201);
  const conversation = (await conversationResponse.json()) as { id: string };
  const blocked = await page.request.post(
    `/api/v1/conversations/${conversation.id}/messages`,
    { data: { question: "What does the policy require?" } },
  );
  expect(blocked.status()).toBe(409);
  expect(((await blocked.json()) as ErrorPayload).error.code).toBe("gemini_consent_required");

  await page.getByRole("link", { name: /Ask/ }).click();
  await expect(page.getByRole("button", { name: "I understand, enable Gemini" })).toBeVisible();
  const consentResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      new URL(response.url()).pathname === `/api/v1/workspaces/${workspace.id}/consent`,
  );
  await page.getByRole("button", { name: "I understand, enable Gemini" }).click();
  expect((await consentResponse).status()).toBe(200);
  await expect(page.getByRole("button", { name: "I understand, enable Gemini" })).toHaveCount(0);

  await page.getByLabel("Business question").fill("What does the policy require?");
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.getByText("rag route", { exact: true })).toBeVisible();
  await page.getByText("Retrieved document sources").click();
  await expect(
    page.locator(".source-list > li").filter({ hasText: "review_policy.pdf, page 1" }),
  ).toBeVisible();

  await page.getByLabel("Business question").fill("Which uploaded transactions violate the policy?");
  await page.getByRole("checkbox", { name: "Require approval before hybrid SQL runs" }).check();
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.getByText("hybrid route", { exact: true })).toBeVisible();
  await expect(page.locator(".sql-block code").filter({ hasText: 'SELECT "merchant"' })).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve SQL" })).toBeVisible();
  const hybridCard = page.locator(".message-pair").last();
  await hybridCard.getByText("What was checked").click();
  await expect(hybridCard).toContainText("structural match");
  await expect(hybridCard).toContainText("1 policy condition(s) compared with the SQL");
  await page.getByRole("button", { name: "Approve SQL" }).click();
  await expect(page.getByRole("button", { name: "Approve SQL" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Download CSV" })).toBeVisible();

  const csvDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download CSV" }).click();
  expect((await csvDownload).suggestedFilename()).toMatch(/\.csv$/);
  const excelDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download Excel" }).click();
  expect((await excelDownload).suggestedFilename()).toMatch(/\.xlsx$/);
  await page.getByRole("link", { name: /Analysis/ }).click();
  const reportDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download Markdown report" }).click();
  expect((await reportDownload).suggestedFilename()).toBe("business-intelligence-report.md");

  await page.getByRole("button", { name: "Reset workspace" }).click();
  await page.getByRole("dialog", { name: "Delete this workspace?" }).getByRole("button", { name: "Delete workspace" }).click();
  await expect(page.getByText("Local workspace ready")).toBeVisible();
});
