import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import type { APIRequestContext, Page } from "@playwright/test";

import { expect, test } from "../test";
import { API_PORT, restartApi } from "./stack";

// UI-6c (#156) against the production API code with SCRIPTED, test-only adapters
// (tests/e2e/investigation_app.py): the real churn sample, the real SQL gateway, verifier, and report
// store, and scripted model answers. It proves browser and API wiring of a full report. It says
// nothing about any production model, which stays unavailable by default.

test.skip(process.env.E2E_REAL_STACK !== "scripted", "requires E2E_REAL_STACK=scripted");
test.describe.configure({ mode: "serial" });
test.setTimeout(300_000);

const API = `http://127.0.0.1:${API_PORT}`;
const SAMPLES = resolve(import.meta.dirname, "../../../../sample_data");
const WORKSPACE_HINT = "conversational-bi.workspace-hint";
const QUESTION = "Why did customer churn increase last quarter?";
const SERVER_STEP = { timeout: 60_000 };
const PDFS = [
  "subscription_churn_pricing_change_notice.pdf",
  "subscription_churn_support_operations_update.pdf",
  "subscription_churn_retention_playbook.pdf",
  "subscription_churn_draft_board_memo.pdf",
];
const MAPPING = {
  amount: "mrr_at_start",
  date: "quarter_start",
  customer_id: "customer_id",
  merchant: null,
  location: null,
  label: null,
};

async function json<T>(response: Awaited<ReturnType<APIRequestContext["get"]>>): Promise<T> {
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as T;
}

/** A workspace with the churn table and four PDFs whose latest message is a proposal. */
async function churnWorkspace(request: APIRequestContext): Promise<string> {
  const workspace = await json<{ id: string }>(
    await request.post(`${API}/api/v1/workspaces`, {
      headers: { "Idempotency-Key": `report-${randomUUID()}` },
    }),
  );
  const upload = await json<{ id: string }>(
    await request.post(`${API}/api/v1/workspaces/${workspace.id}/tabular-uploads`, {
      multipart: {
        file: {
          name: "subscription_churn.csv",
          mimeType: "text/csv",
          buffer: readFileSync(resolve(SAMPLES, "subscription_churn.csv")),
        },
      },
    }),
  );
  const dataset = await json<{ id: string }>(
    await request.post(`${API}/api/v1/tabular-uploads/${upload.id}/dataset`, { data: {} }),
  );
  await json(await request.put(`${API}/api/v1/datasets/${dataset.id}/schema-mapping`, { data: MAPPING }));
  await json(
    await request.put(`${API}/api/v1/workspaces/${workspace.id}/consent`, {
      data: { accepted: true, notice_version: "2026-09" },
    }),
  );
  const files = new FormData();
  for (const name of PDFS) {
    files.append("files", new File([readFileSync(resolve(SAMPLES, name))], name, { type: "application/pdf" }));
  }
  const collection = await json<{ id: string }>(
    await request.post(`${API}/api/v1/workspaces/${workspace.id}/document-collections`, { multipart: files }),
  );
  const conversation = await json<{ id: string }>(
    await request.post(`${API}/api/v1/workspaces/${workspace.id}/conversations`, {
      data: { dataset_id: dataset.id, document_collection_id: collection.id },
    }),
  );
  const message = await json<{ route: string }>(
    await request.post(`${API}/api/v1/conversations/${conversation.id}/messages`, { data: { question: QUESTION } }),
  );
  expect(message.route).toBe("investigation");
  return workspace.id;
}

const status = (page: Page) => page.locator(".investigation-status-row .status-chip").first();

async function axe(page: Page) {
  const result = await new AxeBuilder({ page }).analyze();
  expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(" | ")}`)).toEqual([]);
}

async function confirmIn(page: Page, label: string) {
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: label }).click();
  await expect(dialog).toBeHidden();
}

/** Decides each scripted SQL wave through the real review UI until the run ends. */
async function approveEveryWave(page: Page) {
  const report = page.getByRole("heading", { name: "Verified report" });
  for (let wave = 1; wave <= 8; wave += 1) {
    const heading = page.getByRole("heading", { name: `Review SQL wave ${wave}` });
    await expect(heading.or(report)).toBeVisible(SERVER_STEP);
    if (await report.isVisible()) return;
    const choices = page.getByRole("radio", { name: "Approve as shown" });
    for (let index = 0; index < (await choices.count()); index += 1) await choices.nth(index).check();
    await page.getByRole("button", { name: "Submit decisions" }).click();
    await expect(heading).toBeHidden(SERVER_STEP);
  }
  await expect(report).toBeVisible(SERVER_STEP);
}

test.beforeAll(async () => {
  await restartApi("investigation-report");
});

test.afterAll(async () => {
  await restartApi("retail");
});

test("a scripted churn run reaches a verified report whose claims and evidence link both ways", async ({
  page,
  request,
}) => {
  const workspaceId = await churnWorkspace(request);
  await page.addInitScript(
    `sessionStorage.setItem(${JSON.stringify(WORKSPACE_HINT)}, ${JSON.stringify(workspaceId)})`,
  );
  await page.goto("/#ask");
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();

  await page.getByRole("button", { name: "Start investigation" }).click();
  await expect(page.getByRole("heading", { name: "Review the proposed investigation plan" })).toBeVisible(SERVER_STEP);
  await page.locator(".investigation-plan").getByRole("button", { name: "Approve and run plan" }).click();
  await confirmIn(page, "Approve and run plan");

  await approveEveryWave(page);
  await expect(status(page)).toHaveText(/Completed/, SERVER_STEP);

  const workflow = page.locator(".investigation-workflow");
  await expect(workflow).toContainText("verified, association only");
  await expect(workflow).toContainText("No model wrote this narrative");
  await expect(workflow).toContainText("can return counts that do not reflect the source data");
  await expect(page.locator(".report-claim").first()).toBeVisible();
  await expect(page.getByRole("article", { name: /^Measurement f1/ })).toBeVisible();

  // The inspector reads the run's real evidence.
  await expect(page.getByRole("article", { name: "Hypothesis h1" })).toContainText("Supported association");
  await expect(page.getByRole("article", { name: "Hypothesis h4" })).toBeVisible();
  await expect(workflow).toContainText("A document claim contradicts the verified data");
  await expect(page.locator(".evidence-card pre").first()).toContainText("SELECT");
  await expect(page.getByRole("group", { name: /Rows returned by task/ }).first().getByRole("cell").first()).toBeVisible();
  await expect(page.locator(".evidence-sources").first()).toContainText(".pdf");
  await axe(page);

  // Every measurement links to evidence that is really there; keyboard to it and back again.
  const measurement = page.getByRole("article", { name: /^Measurement f1/ });
  await expect(measurement.getByText("(not available)")).toHaveCount(0);
  await measurement.getByRole("button", { name: /^Inspect evidence/ }).first().focus();
  await page.keyboard.press("Enter");
  const card = page.locator(".evidence-card:focus");
  await expect(card).toHaveCount(1);
  await card.getByRole("button", { name: "Measurement f1" }).focus();
  await page.keyboard.press("Enter");
  await expect(measurement).toBeFocused();

  // Export is the server's verified report, with no SQL.
  const saved = page.waitForEvent("download");
  const exportMarkdown = page.getByRole("button", { name: "Export report as Markdown" });
  await exportMarkdown.focus();
  await page.keyboard.press("Enter");
  const download = await saved;
  expect(download.suggestedFilename()).toMatch(/^investigation-report-inv_[0-9a-f]+\.md$/);
  const text = readFileSync(await download.path(), "utf-8");
  expect(text).toContain("verified, association only");
  expect(text).toContain("Evidence: available");
  expect(text).toContain("can return counts that do not reflect the source data");
  expect(text).toContain("Measurements");
  expect(text).not.toContain("SELECT");
  await expect(exportMarkdown).toBeFocused();

  const savedJson = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export report as JSON" }).click();
  const jsonDownload = await savedJson;
  expect(jsonDownload.suggestedFilename()).toMatch(/^investigation-report-inv_[0-9a-f]+\.json$/);
  const exported = JSON.parse(readFileSync(await jsonDownload.path(), "utf-8")) as Record<string, unknown>;
  expect(exported.evidence_state).toBe("available");
  expect(String(exported.verification_scope)).toContain("do not reflect the source data");
  expect(JSON.stringify(exported)).not.toContain("SELECT");

  // A reload re-reads the durable report; nothing is kept in browser storage.
  await page.reload();
  await expect(page.getByRole("heading", { name: "Verified report" })).toBeVisible(SERVER_STEP);
  await expect(page.getByRole("article", { name: "Hypothesis h1" })).toBeVisible();
  const stored = await page.evaluate("JSON.stringify([{...sessionStorage}, {...localStorage}])");
  expect(stored).not.toContain("SELECT");
  expect(stored).not.toContain("uploaded_data");
});

test("a report that survives an API restart is still digest-checked and shown", async ({ page, request }) => {
  const workspaceId = await churnWorkspace(request);
  await page.addInitScript(
    `sessionStorage.setItem(${JSON.stringify(WORKSPACE_HINT)}, ${JSON.stringify(workspaceId)})`,
  );
  await page.goto("/#ask");
  await page.getByRole("button", { name: "Start investigation" }).click();
  await expect(page.getByRole("heading", { name: "Review the proposed investigation plan" })).toBeVisible(SERVER_STEP);
  await page.locator(".investigation-plan").getByRole("button", { name: "Approve and run plan" }).click();
  await confirmIn(page, "Approve and run plan");
  await approveEveryWave(page);

  await restartApi("investigation-report");
  await page.reload();
  await expect(page.getByRole("heading", { name: "Verified report" })).toBeVisible(SERVER_STEP);
  await expect(page.locator(".evidence-card pre").first()).toContainText("SELECT");
  await axe(page);
});
