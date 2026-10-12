/**
 * Retail demo journeys against the real API and the production web bundle.
 *
 * The model is SCRIPTED (tests/e2e/retail_app.py): it supplies the routes, policy predicates, SQL
 * and quoted sentence a correct model would. Everything else is production code. These tests
 * therefore verify the application and its UI, not the quality of any live model.
 *
 * Run with: npm run test:e2e:real   (E2E_REAL_STACK=scripted)
 * Set E2E_CAPTURE=1 to refresh the screenshots under docs/assets/demo.
 */
import AxeBuilder from "@axe-core/playwright";
import type { Locator, Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

import { expect, test } from "../test";
import { API_PORT, restartApi } from "./stack";

test.skip(process.env.E2E_REAL_STACK !== "scripted", "requires E2E_REAL_STACK=scripted");
test.describe.configure({ mode: "serial" });

const repoRoot = path.resolve(import.meta.dirname, "../../../..");
const screenshotDir = path.join(repoRoot, "docs/assets/demo");
const capture = process.env.E2E_CAPTURE === "1";

const REGION = "Which region has the highest total revenue?";
const TOTAL = "What is total revenue across the dataset?";
const CATEGORY = "Which category generated the most gross profit?";
const MONTH = "Which month had the highest revenue?";
const H1 = "How did first-half revenue change from 2025 to 2026?";
const SEGMENT = "How many transactions have an unknown customer segment?";
const RETURN_RULE = "Does a return by itself require escalation?";
const FULFILLMENT = "How many transactions meet the policy fulfillment escalation rule?";
const REVIEW = "How many transactions meet at least one documented review rule?";

// Values as the interface formats them (thousands separators) for the committed ground truth.
const TABLE_EXPECTATIONS: [string, string[]][] = [
  [TOTAL, ["1,625,730.59"]],
  [REGION, ["West", "533,933.01"]],
  [CATEGORY, ["Electronics", "196,361.54"]],
  [MONTH, ["2025-10", "113,432.96"]],
  [H1, ["503,347.58", "529,303.93"]],
  [SEGMENT, ["24"]],
];

async function shot(target: Page | Locator, name: string) {
  if (!capture) return;
  // A sticky header would overlay element captures, so make it part of the normal flow.
  const page = "page" in target && typeof target.page === "function" ? target.page() : (target as Page);
  await page.addStyleTag({
    content: ".site-header{position:static!important}.skip-link{display:none!important}",
  });
  await target.screenshot({ path: path.join(screenshotDir, `${name}.png`) });
}

const latest = (page: Page) => page.locator(".message-pair").last();

/** Sends the typed question and waits for its answer card, so `latest` never points at the previous one. */
async function submit(page: Page) {
  const before = await page.locator(".message-pair").count();
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.locator(".message-pair")).toHaveCount(before + 1);
}

async function ask(page: Page, question: string, options: { approval?: boolean } = {}) {
  const box = page.getByRole("textbox", { name: "Business question" });
  await expect(box).toBeEnabled();
  await box.fill(question);
  const approval = page.getByRole("checkbox", { name: "Require approval before hybrid SQL runs" });
  if (options.approval) await approval.check();
  else if (await approval.isChecked()) await approval.uncheck();
  await submit(page);
}

async function pickSample(page: Page, question: string) {
  await page.getByRole("button", { name: question }).click();
  await expect(page.getByRole("textbox", { name: "Business question" })).toHaveValue(question);
}

/** Opens the app and waits until the workspace and its history are restored, so counts are stable. */
async function open(page: Page) {
  await page.goto("/");
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
}

const messageCount = (page: Page) => page.locator(".message-pair").count();

async function axe(page: Page) {
  const result = await new AxeBuilder({ page }).exclude(".js-plotly-plot").analyze();
  expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(" | ")}`)).toEqual([]);
}

test("the expectations used here match the committed ground truth", () => {
  const truth = JSON.parse(
    readFileSync(path.join(repoRoot, "sample_data/retail_operations_ground_truth.json"), "utf-8"),
  ) as { questions: { question: string; answer: Record<string, string | number> }[] };
  for (const [question, expected] of TABLE_EXPECTATIONS) {
    const entry = truth.questions.find((item) => item.question === question);
    expect(entry, question).toBeDefined();
    const values = Object.values(entry?.answer ?? {}).map((value) => String(value));
    for (const value of expected) {
      expect(values.some((v) => v === value.replaceAll(",", "") || v === value), value).toBe(true);
    }
  }
});

test("startup explains the product, the demo, and its limits, with a ready workspace", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { level: 1 })).toContainText("evidence in view");
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Limits to keep in mind" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Retail operations review" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Load demo data and policy" })).toBeEnabled();
  await expect(page.getByTestId("retention-notice")).toContainText("hours after its last use");
  // Nothing claims accuracy it cannot back.
  await expect(page.locator("main")).not.toContainText(/hallucination-free|fully autonomous|100% accurate/i);
  await shot(page, "01-home-and-demo-guide");
});

test("demo ingestion goes through schema review; data questions wait for confirmation", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Load demo data and policy" }).click();

  await expect(page.getByRole("heading", { name: "Confirm the canonical schema" })).toBeVisible();
  await expect(page.getByText("Indexed 1 document(s)", { exact: false })).toBeVisible();
  // The real mapper's suggestions for this table: the measure, date, and region are mapped;
  // "customer_segment" is a grouping rather than an ID, and "status" is a derived flag, so the
  // customer ID and label stay unmapped (with the reason shown for the label).
  await expect(page.getByLabel("amount")).toHaveValue("revenue");
  await expect(page.getByLabel("date")).toHaveValue("transaction_date");
  await expect(page.getByLabel("location")).toHaveValue("region");
  await expect(page.getByLabel("customer id")).toHaveValue("");
  await expect(page.getByLabel("label")).toHaveValue("");
  await expect(page.getByText("label is left unmapped for the demo:")).toBeVisible();
  const samples = page.getByRole("region", { name: /Sample questions/ });
  await expect(samples.getByRole("button", { name: REGION })).toBeDisabled();
  // Nothing model-backed is enabled until the data-sharing notice is accepted.
  await expect(samples.getByRole("button", { name: RETURN_RULE })).toBeDisabled();
  await page.getByRole("button", { name: "I understand, enable Gemini" }).click();
  await expect(samples.getByRole("button", { name: RETURN_RULE })).toBeEnabled();
  await expect(samples.getByRole("button", { name: REGION })).toBeDisabled();
  await shot(page.locator("#step-data"), "02-schema-review");

  await page.getByRole("button", { name: "Confirm schema" }).click();
  await expect(page.getByRole("heading", { name: "Configure anomaly review" })).toBeVisible();
  await expect(samples.getByRole("button", { name: REGION })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Demo data loaded" })).toBeDisabled();
});

test("deterministic analysis runs on the demo table", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  await page.getByRole("button", { name: "Run deterministic analysis" }).click();
  // 2,400 rows: anomaly scoring and the classifier take a few seconds.
  await expect(page.getByRole("heading", { name: "Analysis dashboard" })).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.getByText("Anomalies flagged")).toBeVisible();
  await shot(page.locator("#step-data"), "03-deterministic-analysis");
});

test("natural-language SQL answers match the ground truth and show their SQL and rows", async ({ page }) => {
  await page.goto("/");

  for (const [question, expected] of TABLE_EXPECTATIONS) {
    await pickSample(page, question);
    await submit(page);
    const card = latest(page);
    await expect(card.getByText("sql route", { exact: true })).toBeVisible();
    await expect(card.getByRole("heading", { name: "Generated SQL" })).toBeVisible();
    await expect(card.locator(".sql-block code")).toContainText("uploaded_data");
    const table = card.getByRole("region", { name: /Query result/ });
    for (const value of expected) await expect(table).toContainText(value);
  }

  // The last question was the customer-segment count; re-ask the region ranking for the capture.
  await pickSample(page, REGION);
  await submit(page);
  const card = latest(page);
  await expect(card.getByRole("heading", { name: "Calculated from the result table" })).toBeVisible();
  await expect(card.locator(".observation-list")).toContainText("West");
  await expect(card).toContainText("computed by code from the rows below, not written by a model");
  await expect(card.getByRole("button", { name: "Download CSV" })).toBeVisible();
  await shot(card, "04-grounded-sql-insight");

  // Units and ambiguity are stated, not guessed.
  await pickSample(page, TOTAL);
  await submit(page);
  await expect(latest(page).locator(".limit-list")).toContainText("No currency or unit was inferred");
});

test("a document question cites a retrieved passage and says what was and was not checked", async ({ page }) => {
  await page.goto("/");
  await pickSample(page, RETURN_RULE);
  await submit(page);

  const card = latest(page);
  await expect(card.getByText("rag route", { exact: true })).toBeVisible();
  await expect(card.locator(".answer-text")).toContainText("does not by itself require escalation");
  const citation = card.getByRole("link", { name: "Source 1" });
  await expect(citation).toBeVisible();

  await card.getByText("Retrieved document sources").click();
  await expect(card.locator(".source-heading")).toContainText("retail_operations_policy.pdf");
  await expect(card.locator(".source-heading")).toContainText("page 1");
  await expect(card.locator(".source-list blockquote").first()).toContainText("Retail Operations Review Policy");
  await citation.click();
  await expect(card.locator("li[id^='source-']").first()).toBeInViewport();

  await card.getByText("What was checked").click();
  await expect(card.locator(".evidence-list")).toContainText("Reference verified");
  await expect(card.locator(".evidence-list")).toContainText("Meaning not evaluated");
  await expect(card).toContainText("None of them proves that a statement is true");
  await expect(card).toContainText("Decision support only");
  await shot(card, "05-document-citations");
});

test("a hybrid question waits for approval, shows its SQL, then runs only when approved", async ({ page }) => {
  await page.goto("/");
  await pickSample(page, FULFILLMENT);
  await ask(page, FULFILLMENT, { approval: true });

  const card = latest(page);
  await expect(card.getByRole("button", { name: "Approve SQL" })).toBeVisible();
  await expect(card.getByText("Awaiting approval")).toBeVisible();
  await expect(card.locator(".sql-block code")).toContainText('"fulfillment_days" >= 10');
  await expect(card.getByRole("group", { name: "SQL approval decision" })).toBeFocused();
  await expect(card.getByRole("region", { name: /Query result/ })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Business question" })).toBeDisabled();
  await expect(card).toContainText("has not run");
  await shot(card, "06-approval-pending");

  await card.getByRole("button", { name: "Approve SQL" }).click();
  await expect(card.getByRole("button", { name: "Approve SQL" })).toHaveCount(0);
  await expect(card.getByRole("region", { name: /Query result/ })).toContainText("13");
  await expect(card.getByText("hybrid route", { exact: true })).toBeVisible();
  await expect(card).toContainText("structural match, not proof that the query logic is right");
  await expect(page.getByRole("textbox", { name: "Business question" })).toBeEnabled();

  await card.getByText("How this was produced").click();
  await expect(card.locator(".timeline")).toContainText("Query executed");
  await expect(card.locator(".timeline")).toContainText("Document retrieval completed");
  await shot(card, "07-hybrid-policy-answer");
});

test("the review-rule count applies all three documented conditions", async ({ page }) => {
  await page.goto("/");
  await pickSample(page, REVIEW);
  await submit(page);

  const card = latest(page);
  await expect(card.getByRole("region", { name: /Query result/ })).toContainText("45");
  await expect(card.locator(".sql-block code")).toContainText('"discount_rate" >= 0.30');
  await expect(card.locator(".sql-block code")).toContainText('"gross_margin_rate" < 0.10');
  await card.getByText("What was checked").click();
  await expect(card).toContainText("3 policy condition(s) compared with the SQL");
});

test("rejecting a pending approval never executes the SQL", async ({ page }) => {
  await open(page);
  const before = await messageCount(page);
  await ask(page, REVIEW, { approval: true });
  const card = latest(page);
  await expect(card.getByRole("button", { name: "Reject SQL" })).toBeVisible();
  await card.getByRole("button", { name: "Reject SQL" }).click();

  await expect(card.getByText("Not executed")).toBeVisible();
  await expect(card.getByRole("region", { name: /Query result/ })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "Download CSV" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Business question" })).toBeEnabled();
  await expect(page.getByRole("textbox", { name: "Business question" })).toBeFocused();
  expect(await messageCount(page)).toBe(before + 1);
});

test("an edited query is revalidated: a policy-consistent edit runs; a deviating or unsafe one does not", async ({ page }) => {
  await open(page);

  // An edit that keeps the policy predicate and adds no condition is allowed and runs.
  await ask(page, FULFILLMENT, { approval: true });
  let card = latest(page);
  await card.getByRole("button", { name: "Edit SQL" }).click();
  const editor = card.getByLabel("Edit SQL");
  await editor.fill((await editor.inputValue()).replace('AS "transaction_count"', 'AS "escalated_count"'));
  await card.getByRole("button", { name: "Run edited SQL" }).click();
  await expect(card.locator(".sql-block code")).toContainText('AS "escalated_count"');
  await expect(card.getByRole("region", { name: /Query result/ })).toBeVisible();
  await expect(card).toContainText("structural match");

  // An added filter the question never asked for has no authority, so it is refused even though the
  // policy predicate is still present; the system falls back to the document answer.
  await ask(page, FULFILLMENT, { approval: true });
  card = latest(page);
  await card.getByRole("button", { name: "Edit SQL" }).click();
  const extra = card.getByLabel("Edit SQL");
  await extra.fill(`${await extra.inputValue()} AND "region" = 'West'`);
  await card.getByRole("button", { name: "Run edited SQL" }).click();
  await expect(card.getByRole("button", { name: "Run edited SQL" })).toHaveCount(0);
  await expect(card).toContainText("could not be safely turned into a table query");
  await expect(card.getByRole("region", { name: /Query result/ })).toHaveCount(0);

  // Changing the policy's own threshold means the SQL no longer applies the document's rule: the
  // system falls back to the document answer instead of presenting rows as policy results.
  await ask(page, FULFILLMENT, { approval: true });
  card = latest(page);
  await card.getByRole("button", { name: "Edit SQL" }).click();
  await card.getByLabel("Edit SQL").fill((await card.getByLabel("Edit SQL").inputValue()).replace(">= 10", ">= 14"));
  await card.getByRole("button", { name: "Run edited SQL" }).click();
  await expect(card.getByRole("button", { name: "Run edited SQL" })).toHaveCount(0);
  await expect(card).toContainText("could not be safely turned into a table query");
  await expect(card).toContainText("do not match the values extracted from your policy");
  await expect(card.getByRole("region", { name: /Query result/ })).toHaveCount(0);
  await shot(card, "07b-edit-deviates-from-policy");

  // An unsafe edit is refused and nothing runs.
  await ask(page, FULFILLMENT, { approval: true });
  card = latest(page);
  await card.getByRole("button", { name: "Edit SQL" }).click();
  await card.getByLabel("Edit SQL").fill('DELETE FROM "uploaded_data"');
  await card.getByRole("button", { name: "Run edited SQL" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(card.getByText("Not executed")).toBeVisible();
  await expect(card.getByRole("region", { name: /Query result/ })).toHaveCount(0);

  // The table is untouched.
  await pickSample(page, TOTAL);
  await submit(page);
  await expect(latest(page).getByRole("region", { name: /Query result/ })).toContainText("1,625,730.59");
});

test("large, empty, and failing answers are handled without hanging", async ({ page }) => {
  await open(page);

  await ask(page, "Show every transaction.");
  let card = latest(page);
  const table = card.getByRole("region", { name: /Query result/ });
  await expect(table).toBeVisible();
  await expect(table.locator("caption")).toContainText("500 rows (showing the first 25)");
  expect(await card.locator("tbody tr").count()).toBe(25);
  await card.getByRole("button", { name: "Show all 500 rows" }).click();
  expect(await card.locator("tbody tr").count()).toBe(500);

  await ask(page, "Show transactions in the Moon region.");
  card = latest(page);
  await expect(card).toContainText("returned no matching rows");

  await page.getByRole("textbox", { name: "Business question" }).fill("Trigger a provider failure.");
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.getByRole("alert")).toContainText("failed on purpose");
  await expect(page.getByRole("textbox", { name: "Business question" })).toHaveValue(
    "Trigger a provider failure.",
  );
  await expect(page.getByRole("button", { name: "Ask workbench" })).toBeEnabled();
});

test("network failures and slow answers: the question is kept, and repeats are not sent", async ({ page }) => {
  await open(page);
  const messages = "**/api/v1/conversations/*/messages";

  await page.route(messages, (route) => route.abort("failed"));
  await page.getByRole("textbox", { name: "Business question" }).fill(TOTAL);
  await page.getByRole("button", { name: "Ask workbench" }).click();
  await expect(page.getByRole("alert")).toContainText("Could not reach the server");
  await expect(page.getByRole("textbox", { name: "Business question" })).toHaveValue(TOTAL);
  await page.unroute(messages);

  let posts = 0;
  await page.route(messages, async (route) => {
    if (route.request().method() === "POST") {
      posts += 1;
      await new Promise((resolve) => setTimeout(resolve, 1_500));
    }
    await route.continue();
  });
  const before = await messageCount(page);
  await page.getByRole("button", { name: "Ask workbench" }).click();
  const busy = page.getByRole("button", { name: "Analyzing question…" });
  await expect(busy).toBeDisabled();
  await expect(page.getByRole("textbox", { name: "Business question" })).toBeDisabled();
  await expect(latest(page).getByRole("region", { name: /Query result/ })).toContainText("1,625,730.59", {
    timeout: 15_000,
  });
  expect(posts).toBe(1);
  expect(await messageCount(page)).toBe(before + 1);
  await page.unroute(messages);
});

test("an unsupported upload fails with a clear message and the page stays usable", async ({ page }) => {
  await open(page);
  await page.getByLabel("CSV or Excel file").setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("not a table"),
  });
  await page.getByRole("button", { name: "Upload and profile" }).click();
  await expect(page.getByRole("alert")).toContainText("Supported tabular file types");
  await expect(page.getByRole("button", { name: "Upload and profile" })).toBeEnabled();
});

test("a reload restores the workspace, its data, and the conversation", async ({ page }) => {
  await open(page);
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Analysis dashboard" })).toBeVisible();
  await expect(page.getByText("Indexed 1 document(s)", { exact: false })).toBeVisible();
  expect(await messageCount(page)).toBeGreaterThan(8);
  await expect(page.getByRole("button", { name: "Demo data loaded" })).toBeDisabled();
  await shot(page, "08-restored-workspace");

  // The browser keeps one opaque identifier and nothing else.
  const stored = JSON.parse(
    await page.evaluate<string>("JSON.stringify(Object.entries(sessionStorage))"),
  ) as [string, string][];
  expect(stored).toHaveLength(1);
  expect(stored[0]?.[1]).toMatch(/^ws_[0-9a-f]+$/);
  expect(await page.evaluate("localStorage.length")).toBe(0);
});

test("a pending approval is still pending after a reload", async ({ page }) => {
  await open(page);
  await ask(page, FULFILLMENT, { approval: true });
  await expect(latest(page).getByRole("button", { name: "Approve SQL" })).toBeVisible();

  await page.reload();
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  const card = latest(page);
  await expect(card.getByRole("button", { name: "Approve SQL" })).toBeVisible();
  await expect(card.locator(".sql-block code")).toContainText("fulfillment_days");
  await expect(page.getByRole("textbox", { name: "Business question" })).toBeDisabled();

  await card.getByRole("button", { name: "Reject SQL" }).click();
  await expect(card.getByText("Not executed")).toBeVisible();
});

test("after the API restarts, data and history return and a pending approval is cancelled safely", async ({ page }) => {
  await open(page);
  await ask(page, REVIEW, { approval: true });
  await expect(latest(page).getByRole("button", { name: "Approve SQL" })).toBeVisible();
  const before = await messageCount(page);

  await restartApi();
  await page.reload();

  // The API rebuilds the table, analysis, and document index from persisted inputs on first use.
  await expect(page.getByText("Restoring or starting workspace…")).toBeVisible();
  await expect(page.getByText("Your workspace was restored.")).toBeVisible({ timeout: 60_000 });
  expect(await messageCount(page)).toBe(before);
  const card = latest(page);
  await expect(card.getByText("Not executed")).toBeVisible();
  await expect(card).toContainText(/restart/i);
  await expect(card.getByRole("button", { name: "Approve SQL" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Business question" })).toBeEnabled();

  // The rebuilt table and the reopened document index both answer again.
  await ask(page, TOTAL);
  await expect(latest(page).getByRole("region", { name: /Query result/ })).toContainText("1,625,730.59");
  await ask(page, REVIEW);
  await expect(latest(page).getByRole("region", { name: /Query result/ })).toContainText("45");
});

test("a stored workspace identifier is only a hint: a foreign or invented one is never requested", async ({
  page,
  request,
}) => {
  const invented = `ws_${"0".repeat(32)}`;
  await page.addInitScript(`sessionStorage.setItem("conversational-bi.workspace-hint", "${invented}")`);
  const requested: string[] = [];
  page.on("request", (req) => requested.push(req.url()));

  await open(page);
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  expect(requested.filter((url) => url.includes(invented))).toEqual([]);

  const missing = await request.get(`http://127.0.0.1:${API_PORT}/api/v1/workspaces/${invented}/overview`);
  expect(missing.status()).toBe(404);
});

test("the layout holds at tablet and phone widths", async ({ page }) => {
  for (const [width, height] of [[768, 1024], [375, 812]] as const) {
    await page.setViewportSize({ width, height });
    await open(page);
    await expect(page.getByText("Your workspace was restored.")).toBeVisible();
    const overflow = await page.evaluate(
      "document.documentElement.scrollWidth - document.documentElement.clientWidth",
    );
    const offenders = await page.evaluate<string[]>(`Array.from(document.querySelectorAll("body *"))
      .filter((el) => el.getBoundingClientRect().right > document.documentElement.clientWidth + 1
        && !el.closest(".table-scroll, .sql-block, .site-nav"))
      .slice(0, 6)
      .map((el) => el.tagName + "." + el.className + " " + Math.round(el.getBoundingClientRect().right))`);
    expect(overflow, `${width}px page overflow; offenders: ${offenders.join(" | ")}`).toBeLessThanOrEqual(1);
    await page.getByRole("textbox", { name: "Business question" }).scrollIntoViewIfNeeded();
    await expect(page.getByRole("button", { name: "Ask workbench" })).toBeVisible();
    if (width === 375) await shot(page, "09-phone-layout");
  }
});

test("axe finds no violations in the loaded workspace, with answers, at desktop and phone width", async ({ page }) => {
  await open(page);
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  await axe(page);

  await ask(page, FULFILLMENT, { approval: true });
  await expect(latest(page).getByRole("button", { name: "Approve SQL" })).toBeVisible();
  await axe(page);
  await latest(page).getByRole("button", { name: "Approve SQL" }).click();
  await expect(latest(page).getByRole("region", { name: /Query result/ })).toBeVisible();
  await latest(page).getByText("What was checked").click();
  await latest(page).getByText("How this was produced").click();
  await axe(page);

  await page.setViewportSize({ width: 375, height: 812 });
  await axe(page);
});

test("resetting deletes the workspace; an expired reference gets a clear explanation", async ({ page, request }) => {
  await open(page);
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "Reset workspace" }).click();
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await expect(page.locator(".message-pair")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Analysis dashboard" })).toHaveCount(0);

  await page.reload();
  await expect(page.getByText("Your workspace was restored.")).toBeVisible();
  const listed = await request.get(`http://127.0.0.1:${API_PORT}/api/v1/workspaces`);
  const items = ((await listed.json()) as { items: { id: string }[] }).items;
  expect(items).toHaveLength(1);

  // Delete it behind the page's back, as retention expiry would, then reload with the stale hint.
  await request.delete(`http://127.0.0.1:${API_PORT}/api/v1/workspaces/${items[0]?.id}`);
  await page.reload();
  await expect(page.getByText("Your earlier workspace is no longer available.")).toBeVisible();
  await expect(page.getByText("Local workspace ready")).toBeVisible();
});
