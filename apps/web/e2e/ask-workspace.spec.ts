import AxeBuilder from "@axe-core/playwright";
import type { Page, Route } from "@playwright/test";

import { conversationFixture, documentCollectionFixture, messageFixture, workspaceFixture } from "./fixtures";
import { expect, test } from "./test";

// UI-6a (#154) journeys against the production bundle with an intercepted API. These are mocked:
// the scripted real-stack journeys in e2e/real cover the same flows against the production API.

const PHONE = { width: 375, height: 812 };
const QUESTION = "Which transactions need review?";

type Handler = (route: Route) => Promise<void> | void;

interface Api {
  /** Answers POST /conversations/{id}/messages. */
  ask: Handler;
  /** Answers GET /conversations/{id}/messages (the reconciliation read). */
  list: Handler;
  /** Answers POST /messages/{id}/approval. */
  decide: Handler;
  posts: number;
}

function answer(overrides: Record<string, unknown> = {}) {
  return { ...messageFixture, question: QUESTION, created_at: new Date().toISOString(), ...overrides };
}

async function routeApi(page: Page): Promise<Api> {
  const api: Api = {
    ask: (route) => route.fulfill({ status: 201, json: answer() }),
    list: (route) => route.fulfill({ status: 200, json: { conversation_id: conversationFixture.id, messages: [], next_cursor: null } }),
    decide: (route) => route.abort("failed"),
    posts: 0,
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path === "/api/v1/auth/config") await route.fallback();
    else if (method === "GET" && path === "/api/v1/workspaces") await route.fulfill({ status: 200, json: { items: [] } });
    else if (method === "POST" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 201, json: { ...workspaceFixture, consent_accepted: true } });
    } else if (method === "POST" && path.endsWith("/document-collections")) {
      await route.fulfill({ status: 201, json: documentCollectionFixture });
    } else if (method === "POST" && path.endsWith("/conversations")) {
      await route.fulfill({ status: 201, json: conversationFixture });
    } else if (path.endsWith("/messages") && method === "POST") {
      api.posts += 1;
      await api.ask(route);
    } else if (path.endsWith("/messages") && method === "GET") await api.list(route);
    else if (path.endsWith("/approval")) await api.decide(route);
    else await route.abort("failed");
  });
  return api;
}

function listing(messages: unknown[]) {
  return { status: 200, json: { conversation_id: conversationFixture.id, messages, next_cursor: null } };
}

async function openAsk(page: Page, viewport?: { width: number; height: number }) {
  if (viewport) await page.setViewportSize(viewport);
  await page.goto("/#documents");
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await page.getByLabel("PDF documents").setInputFiles({
    name: "policy.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF policy"),
  });
  const indexed = page.waitForResponse((response) => response.url().endsWith("/document-collections"));
  await page.getByRole("button", { name: "Upload and index PDFs" }).click();
  await indexed;
  await expect(page.getByRole("button", { name: "Upload and index PDFs" })).toBeEnabled();
  await page.goto("/#ask");
  await expect(page.getByLabel("Business question")).toBeEnabled();
}

async function expectNoAxeViolations(page: Page) {
  const result = await new AxeBuilder({ page }).analyze();
  expect(result.violations).toEqual([]);
}

test("keyboard-only ask: Enter sends once, the question shows as sending, then the answer", async ({ page }) => {
  const api = await routeApi(page);
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  api.ask = async (route) => {
    await held;
    await route.fulfill({ status: 201, json: answer({ id: "msg-kbd" }) });
  };
  await openAsk(page);

  await page.getByLabel("Business question").focus();
  await page.keyboard.type(QUESTION);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");

  const sending = page.getByRole("article", { name: "Answer in progress" });
  await expect(sending).toHaveAttribute("aria-busy", "true");
  await expect(page.getByLabel("Business question")).toBeDisabled();
  await expectNoAxeViolations(page);

  release();
  await expect(page.locator('[data-message-id="msg-kbd"]')).toBeVisible();
  await expect(sending).toHaveCount(0);
  await expect(page.getByTestId("operation-announcer")).toContainText("Answer ready");
  await expect(page.getByRole("heading", { name: "Question 1 of 1" })).toBeAttached();
  expect(api.posts).toBe(1);
  await expectNoAxeViolations(page);
});

test("a lost connection is reconciled from the server: the stored answer is shown and nothing is re-sent", async ({ page }) => {
  const api = await routeApi(page);
  api.ask = (route) => route.abort("failed");
  api.list = (route) => route.fulfill(listing([answer({ id: "msg-stored" })]));
  await openAsk(page);

  await page.getByLabel("Business question").fill(QUESTION);
  await page.getByRole("button", { name: "Ask workbench" }).click();

  await expect(page.getByText(/server answered despite the connection error/)).toBeVisible();
  await expect(page.locator('[data-message-id="msg-stored"]')).toBeVisible();
  await expect(page.getByLabel("Business question")).toHaveValue("");
  expect(api.posts).toBe(1);
  await expectNoAxeViolations(page);
});

test("an unreadable server pauses asking until a check confirms the outcome", async ({ page }) => {
  const api = await routeApi(page);
  api.ask = (route) => route.abort("failed");
  api.list = (route) => route.abort("failed");
  await openAsk(page);

  await page.getByLabel("Business question").fill(QUESTION);
  await page.getByRole("button", { name: "Ask workbench" }).click();

  const check = page.getByRole("button", { name: "Check server state" });
  await expect(check).toBeVisible();
  await expect(page.getByLabel("Business question")).toBeDisabled();
  await expect(page.getByLabel("Business question")).toHaveValue(QUESTION);
  await expectNoAxeViolations(page);

  api.list = (route) => route.fulfill(listing([]));
  await check.click();
  await expect(check).toHaveCount(0);
  await expect(page.getByRole("article", { name: "Question failed" })).toContainText("Nothing was saved");
  await expect(page.getByLabel("Business question")).toBeEnabled();
  await expect(page.getByLabel("Business question")).toHaveValue(QUESTION);
  expect(api.posts).toBe(1);
});

test("citations: keyboard jump to the passage and back; a missing source is marked, not linked", async ({ page }) => {
  const api = await routeApi(page);
  api.ask = (route) =>
    route.fulfill({
      status: 201,
      json: answer({
        id: "msg-cite",
        route: "rag",
        sql: null,
        rows: null,
        answer: "Sales over 500 need review (Source 1). Refunds follow Source 4.",
        sources: [{ citation: "Source 1 - policy.pdf, page 2, chunk 0: Sales over 500 need review." }],
      }),
    });
  await openAsk(page);
  await page.getByLabel("Business question").fill(QUESTION);
  await page.getByRole("button", { name: "Ask workbench" }).click();

  const link = page.getByRole("link", { name: "Source 1: policy.pdf, page 2" });
  await link.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#source-msg-cite-1")).toBeFocused();
  await expect(page).toHaveURL(/#ask$/);

  await page.getByRole("link", { name: /Back to the answer/ }).focus();
  await page.keyboard.press("Enter");
  await expect(link).toBeFocused();

  await expect(page.getByText("(not among the retrieved passages)")).toBeVisible();
  await expect(page.getByRole("link", { name: /Source 4/ })).toHaveCount(0);
  await expectNoAxeViolations(page);
});

test("SQL approval: a stale decision is explained, and an uncertain one waits for a check", async ({ page }) => {
  const api = await routeApi(page);
  const pending = answer({ id: "msg-sql", status: "pending_approval", rows: null, answer: "" });
  api.ask = (route) => route.fulfill({ status: 201, json: pending });
  await openAsk(page);
  await page.getByLabel("Business question").fill(QUESTION);
  await page.getByLabel("Require approval before hybrid SQL runs").check();
  await page.getByRole("button", { name: "Ask workbench" }).click();

  const approve = page.getByRole("button", { name: "Approve SQL" });
  await expect(approve).toBeVisible();
  await expect(page.getByRole("group", { name: "SQL approval decision" })).toContainText("runs nothing");

  // A lost connection: the request is still pending on the server, so deciding again waits.
  api.decide = (route) => route.abort("failed");
  api.list = (route) => route.fulfill(listing([pending]));
  await approve.click();
  const checkAgain = page.getByRole("button", { name: "Check again" });
  await expect(checkAgain).toBeVisible();
  await expect(approve).toBeDisabled();
  await expectNoAxeViolations(page);
  await checkAgain.click();
  await expect(approve).toBeEnabled();

  // Decided elsewhere in the meantime: the server's 409 is explained and its state shown.
  api.decide = (route) =>
    route.fulfill({
      status: 409,
      json: { error: { code: "sql_approval_not_pending", message: "No longer pending.", request_id: "req-409" } },
    });
  api.list = (route) => route.fulfill(listing([{ ...pending, status: "rejected", answer: "Not approved." }]));
  await approve.click();
  await expect(page.getByRole("alert")).toContainText("already decided or cancelled");
  await expect(page.getByText("Not executed")).toBeVisible();
  await expect(approve).toHaveCount(0);
  await expect(page.getByLabel("Business question")).toBeEnabled();
});

test("phone width: the Ask workspace has no horizontal overflow and passes axe", async ({ page }) => {
  const api = await routeApi(page);
  api.ask = (route) => route.fulfill({ status: 201, json: answer({ id: "msg-phone", sql: "SELECT 1" }) });
  await openAsk(page, PHONE);
  await page.getByLabel("Business question").fill(QUESTION);
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.locator('[data-message-id="msg-phone"]')).toBeVisible();

  const overflow = await page.evaluate(
    "document.documentElement.scrollWidth - document.documentElement.clientWidth",
  );
  expect(Number(overflow)).toBeLessThanOrEqual(0);
  await expectNoAxeViolations(page);
});
