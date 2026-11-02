import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import type { APIRequestContext, Page } from "@playwright/test";

import { expect, test } from "../test";
import { API_PORT, apiDataDir, restartApi } from "./stack";

// PROD-1 S5d journeys: the consent lifecycle in a real browser against the production API code and
// durable store, with SCRIPTED, test-only investigation adapters (tests/e2e/investigation_app.py).
// Nothing here calls a model or proves any provider works. The OpenAI journey uses placeholder
// configuration, so its notice is served while admission still leaves production's fail-closed
// adapters in place.

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
const SERVER_STEP = { timeout: 60_000 };
const SCRIPTED_ACCEPT = "Accept this notice for Gemini and Exact Provider";
const OPENAI_ACCEPT = "Accept this notice for Gemini and OpenAI";

type ApiResponse = Awaited<ReturnType<APIRequestContext["get"]>>;

interface ConsentBody {
  status: string;
  accepted: boolean;
  notice_version: string | null;
  data_recipients: string[];
  notice: { version: string; digest: string } | null;
}

async function json<T>(response: ApiResponse): Promise<T> {
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as T;
}

async function emptyWorkspace(request: APIRequestContext): Promise<string> {
  const workspace = await json<{ id: string }>(
    await request.post(`${API}/api/v1/workspaces`, {
      headers: { "Idempotency-Key": `consent-${randomUUID()}` },
    }),
  );
  return workspace.id;
}

async function readyConversation(request: APIRequestContext, workspaceId: string): Promise<string> {
  const upload = await json<{ id: string }>(
    await request.post(`${API}/api/v1/workspaces/${workspaceId}/tabular-uploads`, {
      multipart: { file: { name: "churn.csv", mimeType: "text/csv", buffer: Buffer.from(CSV) } },
    }),
  );
  const dataset = await json<{ id: string }>(
    await request.post(`${API}/api/v1/tabular-uploads/${upload.id}/dataset`, { data: {} }),
  );
  await json(await request.put(`${API}/api/v1/datasets/${dataset.id}/schema-mapping`, { data: MAPPING }));
  const conversation = await json<{ id: string }>(
    await request.post(`${API}/api/v1/workspaces/${workspaceId}/conversations`, {
      data: { dataset_id: dataset.id },
    }),
  );
  return conversation.id;
}

const consentOf = async (request: APIRequestContext, workspaceId: string) =>
  json<ConsentBody>(await request.get(`${API}/api/v1/workspaces/${workspaceId}/consent`));

const ask = (request: APIRequestContext, conversationId: string) =>
  request.post(`${API}/api/v1/conversations/${conversationId}/messages`, {
    data: { question: QUESTION },
  });

async function openAsk(page: Page, workspaceId: string) {
  await page.addInitScript(
    `sessionStorage.setItem(${JSON.stringify(WORKSPACE_HINT)}, ${JSON.stringify(workspaceId)})`,
  );
  await page.goto("/#ask");
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
}

const panel = (page: Page) => page.getByRole("region", { name: "Workspace data-sharing consent" });
const chip = (page: Page) => panel(page).locator(".consent-status");
const announcer = (page: Page) => page.getByTestId("operation-announcer");
const status = (page: Page) => page.locator(".investigation-status-row .status-chip");

async function axe(page: Page) {
  const result = await new AxeBuilder({ page }).analyze();
  expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(" | ")}`)).toEqual([]);
}

async function withdrawInUi(page: Page) {
  await panel(page).getByRole("button", { name: "Withdraw consent" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Requests already sent are not recalled");
  await dialog.getByRole("button", { name: "Withdraw consent" }).click();
  await expect(chip(page)).toHaveText("Withdrawn", SERVER_STEP);
}

/** A proposal whose consent was given in the browser, ready to start an investigation. */
async function proposalWithBrowserConsent(page: Page, request: APIRequestContext) {
  const workspaceId = await emptyWorkspace(request);
  const conversationId = await readyConversation(request, workspaceId);
  await openAsk(page, workspaceId);
  await panel(page).getByRole("button", { name: SCRIPTED_ACCEPT }).click();
  await expect(chip(page)).toHaveText("Accepted", SERVER_STEP);
  const message = await json<{ route: string }>(await ask(request, conversationId));
  expect(message.route).toBe("investigation");
  await page.reload();
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Start investigation" })).toBeEnabled(SERVER_STEP);
  return workspaceId;
}

async function startToPlan(page: Page) {
  await page.getByRole("button", { name: "Start investigation" }).click();
  await expect(page.getByRole("heading", { name: "Review the proposed investigation plan" })).toBeVisible(SERVER_STEP);
  await expect(status(page)).toHaveText("Awaiting plan approval");
}

async function approvePlan(page: Page) {
  await page.locator(".investigation-plan").getByRole("button", { name: "Approve and run plan" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Approve and run plan" }).click();
  await expect(dialog).toBeHidden();
}

test.beforeAll(async () => {
  await restartApi("investigation-scripted");
});

test.afterAll(async () => {
  await restartApi("retail");
});

test("keyboard acceptance and withdrawal persist across reloads and restart; nothing is assumed", async ({
  page,
  request,
}) => {
  const workspaceId = await emptyWorkspace(request);
  const conversationId = await readyConversation(request, workspaceId);
  await openAsk(page, workspaceId);

  // The complete notice is on screen before anything can be accepted.
  const accept = panel(page).getByRole("button", { name: SCRIPTED_ACCEPT });
  await expect(accept).toBeVisible(SERVER_STEP);
  await expect(chip(page)).toHaveText("Not accepted");
  const notice = panel(page).locator(".consent-notice");
  await expect(notice.getByRole("listitem")).toHaveText(["Gemini", "Exact Provider"]);
  await expect(notice).toContainText("Notice version 2026-09");
  await expect(notice).toContainText("does not recall a request already sent");
  await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByLabel("Business question")).toBeDisabled();
  await axe(page);
  expect((await ask(request, conversationId)).status()).toBe(409);

  // Keyboard only: focus the control and press Enter. Focus lands on the heading when it is gone.
  await accept.focus();
  await page.keyboard.press("Enter");
  await expect(chip(page)).toHaveText("Accepted", SERVER_STEP);
  await expect(announcer(page)).toContainText("Data-sharing consent accepted for Gemini and Exact Provider.");
  await expect(accept).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Workspace data-sharing consent" })).toBeFocused();
  await expect(page.getByLabel("Business question")).toBeEnabled();
  await expect(page.locator("[data-testid='operation-announcer']")).toHaveCount(1);
  await axe(page);

  // The notice stays readable with the keyboard.
  const summary = panel(page).getByText("Read the current notice");
  await summary.focus();
  await page.keyboard.press("Enter");
  await expect(panel(page).locator(".consent-details .consent-notice")).toBeVisible();

  await page.reload();
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  await expect(chip(page)).toHaveText("Accepted", SERVER_STEP);

  // Withdrawal needs a confirmation; Escape changes nothing.
  await panel(page).getByRole("button", { name: "Withdraw consent" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await axe(page);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(chip(page)).toHaveText("Accepted");
  expect((await consentOf(request, workspaceId)).status).toBe("current");

  await withdrawInUi(page);
  await expect(announcer(page)).toContainText("Data-sharing consent withdrawn.");
  await expect(panel(page)).toContainText("Requests sent before the withdrawal were not recalled");
  await expect(page.getByLabel("Business question")).toBeDisabled();
  await axe(page);
  expect((await consentOf(request, workspaceId)).status).toBe("withdrawn");
  expect((await ask(request, conversationId)).status()).toBe(409);

  await page.reload();
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  await expect(chip(page)).toHaveText("Withdrawn", SERVER_STEP);

  // The withdrawal is durable: the API process restarts on the same data directory.
  await restartApi("investigation-scripted");
  await page.reload();
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  await expect(chip(page)).toHaveText("Withdrawn", SERVER_STEP);

  await panel(page).getByRole("button", { name: SCRIPTED_ACCEPT }).click();
  await expect(chip(page)).toHaveText("Accepted", SERVER_STEP);
  expect((await consentOf(request, workspaceId)).status).toBe("current");
});

test("a withdrawal made elsewhere shows on reopening Ask, not the remembered state", async ({
  page,
  request,
}) => {
  const workspaceId = await emptyWorkspace(request);
  await openAsk(page, workspaceId);
  await panel(page).getByRole("button", { name: SCRIPTED_ACCEPT }).click();
  await expect(chip(page)).toHaveText("Accepted", SERVER_STEP);

  // Another tab withdraws. Coming back to Ask reads the server rather than the remembered state.
  await json(await request.delete(`${API}/api/v1/workspaces/${workspaceId}/consent`));
  await page.getByRole("link", { name: /Overview/ }).click();
  await page.getByRole("link", { name: /Ask/ }).click();
  await expect(chip(page)).toHaveText("Withdrawn", SERVER_STEP);
});

test("consent the browser cannot read keeps the composer locked; a retry recovers it", async ({
  page,
  request,
}) => {
  const workspaceId = await emptyWorkspace(request);
  await readyConversation(request, workspaceId);
  await json(
    await request.put(`${API}/api/v1/workspaces/${workspaceId}/consent`, {
      data: { accepted: true, notice_version: "2026-09" },
    }),
  );
  const consentPath = `**/api/v1/workspaces/${workspaceId}/consent`;
  await page.route(consentPath, (route) => route.abort());
  await openAsk(page, workspaceId);

  await expect(page.getByText("consent could not be verified", { exact: false })).toBeVisible(SERVER_STEP);
  await expect(page.getByLabel("Business question")).toBeDisabled();
  await expect(chip(page)).toHaveCount(0);
  await axe(page);

  await page.unroute(consentPath);
  await panel(page).getByRole("button", { name: "Read consent again" }).click();
  await expect(chip(page)).toHaveText("Accepted", SERVER_STEP);
  await expect(page.getByLabel("Business question")).toBeEnabled();

  // Leaves nothing accepted for the next journey's workspace rediscovery to find.
  await page.goto("about:blank");
  expect((await request.delete(`${API}/api/v1/workspaces/${workspaceId}`)).ok()).toBe(true);
});

test("the next workspace never inherits the previous workspace's consent", async ({ page, request }) => {
  const workspaceId = await emptyWorkspace(request);
  await openAsk(page, workspaceId);
  await panel(page).getByRole("button", { name: SCRIPTED_ACCEPT }).click();
  await expect(chip(page)).toHaveText("Accepted", SERVER_STEP);

  await page.getByRole("button", { name: "Reset workspace" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete workspace" }).click();
  await expect(page.getByText("Local workspace ready")).toBeVisible(SERVER_STEP);
  await page.getByRole("link", { name: /Ask/ }).click();

  // The app rediscovers another workspace of this caller, or starts a new one. Either way the
  // panel shows the server's state for the workspace it now holds, not the deleted one's "Accepted".
  await expect(chip(page)).not.toHaveText("Accepted", SERVER_STEP);
  const nextId = await page.evaluate((key) => sessionStorage.getItem(key), WORKSPACE_HINT);
  expect(nextId).not.toBeNull();
  expect(nextId).not.toBe(workspaceId);
  const server = await consentOf(request, nextId ?? "");
  expect(server.status).not.toBe("current");
  await expect(chip(page)).toHaveText(server.status === "withdrawn" ? "Withdrawn" : "Not accepted");
  await expect(panel(page).getByRole("button", { name: "Withdraw consent" })).toHaveCount(0);
});

test("withdrawal while a plan awaits approval refuses the approval and sends nothing", async ({
  page,
  request,
}) => {
  await proposalWithBrowserConsent(page, request);
  await startToPlan(page);

  await withdrawInUi(page);
  await approvePlan(page);

  await expect(status(page)).toHaveText("Failed", SERVER_STEP);
  await expect(page.getByText("data-sharing consent or access", { exact: false })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Review SQL wave/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Approve and run plan" })).toHaveCount(0);
  await axe(page);
});

test("withdrawal while a SQL wave awaits a decision stops the run; acceptance never restores it", async ({
  page,
  request,
}) => {
  const workspaceId = await proposalWithBrowserConsent(page, request);
  await startToPlan(page);
  await approvePlan(page);
  await expect(page.getByRole("heading", { name: "Review SQL wave 1" })).toBeVisible(SERVER_STEP);

  await withdrawInUi(page);
  await page.getByRole("radio", { name: "Approve as shown" }).check();
  await page.getByRole("button", { name: "Submit decisions" }).click();

  await expect(status(page)).toHaveText("Cancelled", SERVER_STEP);
  await expect(page.getByText("data-sharing consent or access", { exact: false })).toBeVisible();
  await expect(page.locator(".investigation-wave")).toHaveCount(0);

  await panel(page).getByRole("button", { name: SCRIPTED_ACCEPT }).click();
  await expect(chip(page)).toHaveText("Accepted", SERVER_STEP);
  await page.reload();
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  await expect(status(page)).toHaveText("Cancelled", SERVER_STEP);
  await expect(page.locator(".investigation-wave")).toHaveCount(0);
  expect((await consentOf(request, workspaceId)).status).toBe("current");
  expect(
    existsSync(resolve(apiDataDir("investigation-scripted"), "model-calls.log")),
    "the ordinary model must not be called",
  ).toBe(false);
});

test("OpenAI configured: older consent is stale, the new notice discloses it, and acceptance activates nothing", async ({
  page,
  request,
}) => {
  await restartApi("consent-openai");
  const workspaceId = await emptyWorkspace(request);
  const conversationId = await readyConversation(request, workspaceId);

  // What an earlier client recorded: valid only for the recipients that version disclosed.
  await json(
    await request.put(`${API}/api/v1/workspaces/${workspaceId}/consent`, {
      data: { accepted: true, notice_version: "2026-09" },
    }),
  );
  const older = await consentOf(request, workspaceId);
  expect(older.status).toBe("stale");
  expect(older.data_recipients).toEqual(["Gemini"]);
  expect(older.notice?.version).toBe("2026-10-openai-1");

  await openAsk(page, workspaceId);
  await expect(chip(page)).toHaveText("Needs a new acceptance", SERVER_STEP);
  await expect(panel(page)).toContainText("authorizes only Gemini");
  const notice = panel(page).locator(".consent-notice");
  await expect(notice.getByRole("listitem")).toHaveText(["Gemini", "OpenAI"]);
  for (const disclosure of [
    "external processor",
    "background mode",
    "about ten minutes",
    "not a guaranteed deletion deadline",
    "does not turn on OpenAI processing",
  ]) {
    await expect(notice).toContainText(disclosure);
  }
  await axe(page);

  // The server, not the browser, decides what can be accepted.
  for (const [body, code] of [
    [{ accepted: true, notice_version: "2026-10-openai-0" }, "consent_notice_unknown"],
    [{ accepted: true, notice_version: "2026-10-openai-1" }, "consent_notice_changed"],
    [
      { accepted: true, notice_version: "2026-10-openai-1", notice_digest: "0".repeat(64) },
      "consent_notice_changed",
    ],
  ] as const) {
    const refused = await request.put(`${API}/api/v1/workspaces/${workspaceId}/consent`, { data: body });
    expect(refused.status()).toBe(409);
    expect(((await refused.json()) as { error: { code: string } }).error.code).toBe(code);
  }
  const escalation = await request.put(`${API}/api/v1/workspaces/${workspaceId}/consent`, {
    data: { accepted: true, notice_version: "2026-09", recipients: ["OpenAI"] },
  });
  expect(escalation.status()).toBe(422);
  expect((await consentOf(request, workspaceId)).data_recipients).toEqual(["Gemini"]);

  await panel(page).getByRole("button", { name: OPENAI_ACCEPT }).click();
  await expect(chip(page)).toHaveText("Accepted", SERVER_STEP);
  await expect(announcer(page)).toContainText("Data-sharing consent accepted for Gemini and OpenAI.");
  const accepted = await consentOf(request, workspaceId);
  expect(accepted.notice_version).toBe("2026-10-openai-1");
  expect(accepted.data_recipients).toEqual(["Gemini", "OpenAI"]);

  // Accepting turned nothing on: the investigation still fails closed, with no model call.
  const message = await json<{ route: string }>(await ask(request, conversationId));
  expect(message.route).toBe("investigation");
  await page.reload();
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  await page.getByRole("button", { name: "Start investigation" }).click();
  await expect(status(page)).toHaveText("Failed", SERVER_STEP);
  await expect(
    page.getByText("No model is configured that can run investigations within enforced limits."),
  ).toBeVisible();
  expect(existsSync(resolve(apiDataDir("consent-openai"), "model-calls.log"))).toBe(false);
  await restartApi("investigation-scripted");
});
