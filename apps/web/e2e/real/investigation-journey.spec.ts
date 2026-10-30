import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import type { APIRequestContext, Page } from "@playwright/test";

import { expect, test } from "../test";
import { API_PORT, apiDataDir, restartApi } from "./stack";

// UI-6b (#155) journeys against the production API code with SCRIPTED, test-only bounded
// investigation adapters (tests/e2e/investigation_app.py). They prove browser and API wiring, not
// production model support: production execution stays unavailable until PROD-1 (#198), and
// investigation routing is off by default. The last journey runs the production configuration.

test.skip(process.env.E2E_REAL_STACK !== "scripted", "requires E2E_REAL_STACK=scripted");
test.describe.configure({ mode: "serial" });
test.setTimeout(180_000);

const API = `http://127.0.0.1:${API_PORT}`;
const WORKSPACE_HINT = "conversational-bi.workspace-hint";
const QUESTION = "Why did customer churn increase last quarter?";
const CSV =
  "transaction_date,amount,merchant,customer_id,quarter,segment,churned\n" +
  "2026-01-01,10,Shop A,C-1,Q1,smb,1\n" +
  "2026-01-02,75,Shop B,C-2,Q1,ent,0\n" +
  "2026-04-01,20,Shop A,C-3,Q2,smb,1\n" +
  "2026-04-02,30,Shop C,C-4,Q2,ent,1\n";
const MAPPING = {
  amount: "amount",
  date: "transaction_date",
  customer_id: "customer_id",
  merchant: "merchant",
  location: null,
  label: null,
};
// Scripted planning and SQL take a few bounded polls; a cold first job also imports models.
const SERVER_STEP = { timeout: 60_000 };

async function json<T>(response: Awaited<ReturnType<APIRequestContext["get"]>>): Promise<T> {
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as T;
}

/** A fresh workspace whose latest message is an INV-9 proposal, created only through the API. */
async function proposalWorkspace(request: APIRequestContext): Promise<string> {
  const workspace = await json<{ id: string }>(
    await request.post(`${API}/api/v1/workspaces`, {
      headers: { "Idempotency-Key": `investigation-${randomUUID()}` },
    }),
  );
  const upload = await json<{ id: string }>(
    await request.post(`${API}/api/v1/workspaces/${workspace.id}/tabular-uploads`, {
      multipart: { file: { name: "churn.csv", mimeType: "text/csv", buffer: Buffer.from(CSV) } },
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
  const conversation = await json<{ id: string }>(
    await request.post(`${API}/api/v1/workspaces/${workspace.id}/conversations`, {
      data: { dataset_id: dataset.id },
    }),
  );
  const message = await json<{ route: string }>(
    await request.post(`${API}/api/v1/conversations/${conversation.id}/messages`, {
      data: { question: QUESTION },
    }),
  );
  expect(message.route).toBe("investigation");
  return workspace.id;
}

/** Opens Ask on that workspace; the client discovers (read-only) that nothing has started. */
async function openProposal(page: Page, workspaceId: string) {
  await page.addInitScript(
    `sessionStorage.setItem(${JSON.stringify(WORKSPACE_HINT)}, ${JSON.stringify(workspaceId)})`,
  );
  await page.goto("/#ask");
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Start investigation" })).toBeEnabled(SERVER_STEP);
}

const status = (page: Page) => page.locator(".investigation-status-row .status-chip");

async function startAndReachPlan(page: Page) {
  await page.getByRole("button", { name: "Start investigation" }).click();
  const heading = page.getByRole("heading", { name: "Review the proposed investigation plan" });
  await expect(heading).toBeVisible(SERVER_STEP);
  await expect(heading).toBeFocused();
  await expect(status(page)).toHaveText("Awaiting plan approval");
}

async function confirmIn(page: Page, label: string) {
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: label }).click();
  await expect(dialog).toBeHidden();
}

async function axe(page: Page) {
  const result = await new AxeBuilder({ page }).analyze();
  expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(" | ")}`)).toEqual([]);
}

test.beforeAll(async () => {
  await restartApi("investigation-scripted");
});

test.afterAll(async () => {
  await restartApi("retail");
});

test("creation, plan approval, a SQL wave, and a completed investigation without the report", async ({
  page,
  request,
}) => {
  await openProposal(page, await proposalWorkspace(request));
  await startAndReachPlan(page);

  const plan = page.locator(".investigation-plan");
  await expect(plan).toContainText("Planned tasks");
  await expect(plan).toContainText("trend");
  await expect(plan).toContainText(/Estimated planning cost: up to \$[\d.]+ under the current enforced provider limits\./);
  await expect(plan.getByText("Plan decision deadline")).toBeVisible();
  await axe(page);

  await page.locator(".investigation-plan").getByRole("button", { name: "Approve and run plan" }).click();
  await confirmIn(page, "Approve and run plan");

  const wave = page.getByRole("heading", { name: "Review SQL wave 1" });
  await expect(wave).toBeVisible(SERVER_STEP);
  await expect(wave).toBeFocused();
  await expect(page.getByRole("heading", { name: "Task-owner progress" })).toBeVisible();
  await expect(page.locator(".investigation-owner-progress")).toContainText("sql analyst");
  await expect(page.locator(".investigation-wave pre")).toContainText("SELECT");
  await axe(page);

  const submit = page.getByRole("button", { name: "Submit decisions" });
  await expect(submit).toBeDisabled();
  await page.getByRole("radio", { name: "Approve as shown" }).check();
  await expect(page.locator(".investigation-wave-review")).toContainText("1 approve; 0 reject; 0 edit; 0 undecided.");
  await submit.click();

  await expect(status(page)).toHaveText("Completed", SERVER_STEP);
  await expect(page.getByText("is not shown here")).toBeVisible();
  await expect(page.locator(".investigation-wave")).toHaveCount(0);
  await expect(page.locator(".investigation-workflow")).not.toContainText("SELECT");
  await expect(page.getByRole("button", { name: "Cancel investigation" })).toHaveCount(0);
  await axe(page);
});

test("rejecting the plan is terminal and runs nothing", async ({ page, request }) => {
  await openProposal(page, await proposalWorkspace(request));
  await startAndReachPlan(page);

  await page.locator(".investigation-plan").getByRole("button", { name: "Reject plan" }).click();
  await confirmIn(page, "Reject plan");

  await expect(status(page)).toHaveText("Plan rejected", SERVER_STEP);
  await expect(page.getByRole("heading", { name: "Investigation status" })).toBeFocused();
  await expect(page.getByRole("button", { name: /Approve and run plan|Cancel investigation/ })).toHaveCount(0);
});

test("cancelling while awaiting plan approval is confirmed by the server", async ({ page, request }) => {
  await openProposal(page, await proposalWorkspace(request));
  await startAndReachPlan(page);

  await page.getByRole("button", { name: "Cancel investigation" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("already in flight may finish");
  await confirmIn(page, "Cancel investigation");

  await expect(status(page)).toHaveText("Cancelled", SERVER_STEP);
  await expect(page.getByText("Cancellation is confirmed by the server.")).toBeVisible();
  await expect(page.getByText("This investigation was cancelled.", { exact: false })).toBeVisible();
});

test("an API restart interrupts a pending plan; nothing resumes and no decision is offered", async ({
  page,
  request,
}) => {
  const workspaceId = await proposalWorkspace(request);
  await openProposal(page, workspaceId);
  await startAndReachPlan(page);

  await restartApi("investigation-scripted");
  await page.reload();
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();

  await expect(status(page)).toHaveText("Interrupted", SERVER_STEP);
  await expect(page.getByText("Nothing resumes automatically", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: /Approve and run plan|Reject plan|Cancel investigation|Start investigation/ })).toHaveCount(0);
  await axe(page);
});

test("production configuration: planning fails closed as metering_unbounded with no model call", async ({
  page,
  request,
}) => {
  await restartApi("investigation-production");
  const calls = resolve(apiDataDir("investigation-production"), "model-calls.log");
  await openProposal(page, await proposalWorkspace(request));

  await page.getByRole("button", { name: "Start investigation" }).click();
  await expect(status(page)).toHaveText("Failed", SERVER_STEP);
  await expect(
    page.getByText("No model is configured that can run investigations within enforced limits."),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Review the proposed investigation plan" })).toHaveCount(0);
  await expect(page.getByText(/planning cost/i)).toHaveCount(0);
  expect(existsSync(calls), "the ordinary model must not be called").toBe(false);
  await axe(page);
});
