import AxeBuilder from "@axe-core/playwright";
import type { Page, Route } from "@playwright/test";

import { conversationFixture, documentCollectionFixture, messageFixture, workspaceFixture } from "./fixtures";
import { expect, test } from "./test";

// UI-6b (#155) investigation journeys against the production bundle with an intercepted API.
// These are mocked: e2e/real/investigation-journey.spec.ts covers creation, plan approval, a SQL
// wave, cancellation, restart, and the fail-closed production configuration against the real API.

const PHONE = { width: 375, height: 812 };
const VIEWPORTS = [
  ["desktop", undefined],
  ["phone", PHONE],
] as const;
const INVESTIGATION = "inv-e2e-1";
const JOB = "job-e2e-1";
const PROPOSAL = "msg-proposal-1";
const DIGEST = "a".repeat(64);
const WAVE = "1".repeat(32);
const WAVE_DIGEST = "c".repeat(64);
const SQL_TEXT = {
  trend: 'SELECT "quarter", AVG("churned") FROM "uploaded_data" GROUP BY "quarter"',
  segments: 'SELECT "segment", COUNT(*) FROM "uploaded_data" GROUP BY "segment"',
  regions: 'SELECT "region", COUNT(*) FROM "uploaded_data" GROUP BY "region"',
} as const;
const TASK_IDS = Object.keys(SQL_TEXT) as (keyof typeof SQL_TEXT)[];

type Handler = (route: Route) => Promise<void> | void;
type Investigation = Record<string, unknown> & { status: string };

let clock = 0;
/** Every server state gets a new timestamp, as the real record's `updated_at` would. */
const stamp = () => new Date(Date.UTC(2026, 9, 30, 10, 0, clock++)).toISOString();

function investigation(overrides: Record<string, unknown> = {}): Investigation {
  return {
    id: INVESTIGATION,
    conversation_id: conversationFixture.id,
    message_id: PROPOSAL,
    status: "planning",
    plan: null,
    plan_digest: null,
    authority_epoch: null,
    sql_wave: null,
    explanation: null,
    explanation_code: null,
    report: null,
    proposal: null,
    job_id: JOB,
    progress: null,
    created_at: "2026-10-30T10:00:00Z",
    updated_at: stamp(),
    ...overrides,
  };
}

const PLAN = {
  schema_version: 1,
  objective: "Explain why churn increased last quarter.",
  sub_questions: ["How much did churn change?", "Which segments contributed most?"],
  tasks: TASK_IDS.map((id, index) => ({
    id,
    kind: "sql_analysis",
    assignee: "sql_analyst",
    depends_on: index === 0 ? [] : ["trend"],
    success_criterion: `Counts for ${id}.`,
  })),
};

function pendingPlan(overrides: Record<string, unknown> = {}) {
  return investigation({
    status: "awaiting_plan_approval",
    plan: PLAN,
    plan_digest: DIGEST,
    authority_epoch: 3,
    proposal: {
      cost_estimate: {
        kind: "estimate",
        basis: "token_limits_x_highest_eligible_price",
        bound: "enforced_upper_bound",
        amount_usd: 0.0125,
        limitations: [],
      },
      missing_information: ["Prior-year churn is not in the uploaded table."],
      approval_requirements: [{ kind: "plan", status: "required" }],
      plan_expires_at: "2026-10-30T12:00:00Z",
    },
    ...overrides,
  });
}

function progress(statuses: Record<keyof typeof SQL_TEXT, string>) {
  const values = Object.values(statuses);
  const count = (status: string) => values.filter((value) => value === status).length;
  return {
    total_tasks: values.length,
    completed_tasks: count("succeeded"),
    tasks: TASK_IDS.map((id) => ({
      task_id: id,
      kind: "sql_analysis",
      assignee: "sql_analyst",
      status: statuses[id],
      attempts: statuses[id] === "pending" ? 0 : 1,
    })),
    task_owners: [
      {
        assignee: "sql_analyst",
        total_tasks: values.length,
        completed_tasks: count("succeeded"),
        running_tasks: count("running"),
        pending_tasks: count("pending"),
      },
    ],
  };
}

const running = (overrides: Record<string, unknown> = {}) =>
  investigation({
    status: "running",
    plan: PLAN,
    progress: progress({ trend: "succeeded", segments: "running", regions: "pending" }),
    ...overrides,
  });

const waveSummary = {
  wave_id: WAVE,
  wave_digest: WAVE_DIGEST,
  sequence: 1,
  deadline: "2026-10-30T11:00:00Z",
  items: TASK_IDS.map((id, index) => ({
    task_id: id,
    attempt: 1,
    sql_fingerprint: String(index + 4).repeat(64),
    policy_status: "allowed_read_only",
  })),
};

const awaitingSql = () =>
  investigation({
    status: "awaiting_sql_approval",
    plan: PLAN,
    sql_wave: waveSummary,
    progress: progress({ trend: "running", segments: "running", regions: "running" }),
  });

const waveReview = () => ({
  ...waveSummary,
  items: waveSummary.items.map((item) => ({ ...item, sql: SQL_TEXT[item.task_id] })),
});

function job(status = "running") {
  return {
    id: JOB,
    workspace_id: workspaceFixture.id,
    operation: "investigation_execute",
    status,
    attempt: 1,
    max_attempts: 1,
    cancel_requested: false,
    retryable: false,
    error_category: null,
    progress_completed: 0,
    progress_total: null,
    progress_stage: null,
    request_id: "req-job",
    created_at: "2026-10-30T10:00:00Z",
    updated_at: "2026-10-30T10:00:01Z",
    expires_at: "2026-10-31T10:00:00Z",
  };
}

function proposalMessage(id = PROPOSAL) {
  return {
    ...messageFixture,
    id,
    question: "Why did customer churn increase, and which segments contributed most?",
    answer: "A multi-step investigation is recommended. No investigation has started.",
    route: "investigation",
    sources: [],
    proposal: {
      type: "investigation_proposed",
      schema_version: 1,
      summary: "A multi-step investigation is recommended. No investigation has started.",
      request_reference: id,
      reason_codes: ["feature_enabled", "authorized_table_present", "metric_change_causal"],
      status: "not_started",
      approval_required: true,
    },
    created_at: new Date().toISOString(),
  };
}

const notFound = { status: 404, json: { error: { code: "not_found", message: "Not found.", request_id: "req-404" } } };

interface Api {
  /** The investigation the server holds; null means the by-message lookup is 404. */
  server: Investigation | null;
  byMessage: Handler;
  create: Handler;
  plan: Handler;
  cancel: Handler;
  waveGet: Handler;
  wavePost: Handler;
  job: Handler;
  nextProposal: string;
  counts: Record<"create" | "plan" | "cancel" | "wave", number>;
  bodies: { plan: unknown[]; wave: unknown[] };
}

async function routeApi(page: Page): Promise<Api> {
  const api: Api = {
    server: null,
    byMessage: (route) => (api.server ? route.fulfill({ status: 200, json: api.server }) : route.fulfill(notFound)),
    create: (route) => {
      api.server = investigation();
      return route.fulfill({ status: 202, json: api.server });
    },
    plan: (route) => {
      api.server = running();
      return route.fulfill({ status: 200, json: api.server });
    },
    cancel: (route) => route.fulfill({ status: 202, json: api.server }),
    waveGet: (route) => route.fulfill({ status: 200, json: waveReview() }),
    wavePost: (route) => {
      api.server = running();
      return route.fulfill({ status: 202, json: api.server });
    },
    job: (route) => route.fulfill({ status: 200, json: job() }),
    nextProposal: PROPOSAL,
    counts: { create: 0, plan: 0, cancel: 0, wave: 0 },
    bodies: { plan: [], wave: [] },
  };
  let workspaces = 0;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path === "/api/v1/auth/config") await route.fallback();
    else if (method === "GET" && path === "/api/v1/workspaces") await route.fulfill({ status: 200, json: { items: [] } });
    else if (method === "POST" && path === "/api/v1/workspaces") {
      workspaces += 1;
      const id = workspaces === 1 ? workspaceFixture.id : `workspace-${workspaces}`;
      await route.fulfill({ status: 201, json: { ...workspaceFixture, id, consent_accepted: true } });
    } else if (method === "DELETE" && path.startsWith("/api/v1/workspaces/")) await route.fulfill({ status: 204 });
    else if (method === "POST" && path.endsWith("/document-collections")) {
      await route.fulfill({ status: 201, json: documentCollectionFixture });
    } else if (method === "POST" && path.endsWith("/conversations")) {
      await route.fulfill({ status: 201, json: conversationFixture });
    } else if (method === "POST" && path.endsWith("/messages")) {
      await route.fulfill({ status: 201, json: proposalMessage(api.nextProposal) });
    } else if (method === "GET" && path.endsWith("/messages")) {
      await route.fulfill({ status: 200, json: { conversation_id: conversationFixture.id, messages: [], next_cursor: null } });
    } else if (path.includes("/investigations/by-message/")) await api.byMessage(route);
    else if (method === "POST" && path.endsWith("/investigations")) {
      api.counts.create += 1;
      await api.create(route);
    } else if (path.endsWith("/plan-decision")) {
      api.counts.plan += 1;
      api.bodies.plan.push(request.postDataJSON());
      await api.plan(route);
    } else if (path.endsWith("/cancel")) {
      api.counts.cancel += 1;
      await api.cancel(route);
    } else if (path.endsWith("/sql-wave") && method === "GET") await api.waveGet(route);
    else if (path.endsWith("/sql-wave") && method === "POST") {
      api.counts.wave += 1;
      api.bodies.wave.push(request.postDataJSON());
      await api.wavePost(route);
    } else if (method === "GET" && path === `/api/v1/investigations/${INVESTIGATION}`) {
      await (api.server ? route.fulfill({ status: 200, json: api.server }) : route.fulfill(notFound));
    } else if (method === "GET" && path.startsWith("/api/v1/jobs/")) await api.job(route);
    else await route.abort("failed");
  });
  return api;
}

/** Indexes a document, opens Ask, and asks one question whose stored answer is the proposal. */
async function openProposal(page: Page, viewport?: { width: number; height: number }) {
  if (viewport) await page.setViewportSize(viewport);
  await page.goto("/#documents");
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await page.getByLabel("PDF documents").setInputFiles({
    name: "policy.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF policy"),
  });
  await page.getByRole("button", { name: "Upload and index PDFs" }).click();
  await expect(page.getByRole("button", { name: "Upload and index PDFs" })).toBeEnabled();
  await page.goto("/#ask");
  await askQuestion(page);
}

async function askQuestion(page: Page) {
  const box = page.getByLabel("Business question");
  await expect(box).toBeEnabled();
  await box.fill("Why did customer churn increase, and which segments contributed most?");
  await page.getByRole("button", { name: "Ask workbench" }).click();
}

const panel = (page: Page) => page.locator(".investigation-workflow");
const status = (page: Page) => page.locator(".investigation-status-row .status-chip");
const planHeading = (page: Page) => page.getByRole("heading", { name: "Review the proposed investigation plan" });
const checkServer = (page: Page) => page.getByRole("button", { name: "Check server state" });

async function expectNoAxeViolations(page: Page) {
  const result = await new AxeBuilder({ page }).analyze();
  expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(" | ")}`)).toEqual([]);
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth");
  expect(Number(overflow)).toBeLessThanOrEqual(0);
}

/** Keyboard only: focus the control, press Enter; in the dialog Cancel is focused, Tab confirms. */
async function pressAndConfirm(page: Page, control: ReturnType<Page["getByRole"]>, confirmLabel: string) {
  await control.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
  await expectNoAxeViolations(page);
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: confirmLabel })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(dialog).toBeHidden();
}

async function reachPlanReview(page: Page, api: Api) {
  const start = page.getByRole("button", { name: "Start investigation" });
  await expect(start).toBeEnabled();
  await start.focus();
  await page.keyboard.press("Enter");
  await expect(status(page)).toHaveText("Planning");
  await expect(panel(page)).toContainText("No plan has been approved or executed.");
  await expect(planHeading(page)).toHaveCount(0);
  api.server = pendingPlan();
  await expect(planHeading(page)).toBeFocused();
}

for (const [name, viewport] of VIEWPORTS) {
  test(`${name}: keyboard-only start, planning, full plan review, and approval with task-owner progress`, async ({ page }) => {
    const api = await routeApi(page);
    await openProposal(page, viewport);

    await expect(panel(page)).toContainText("Starting authorizes planning only");
    await expectNoAxeViolations(page);
    await reachPlanReview(page, api);
    expect(api.counts.create).toBe(1);
    await expectNoAxeViolations(page);

    const plan = page.locator(".investigation-plan");
    await expect(plan).toContainText("Explain why churn increased last quarter.");
    await expect(plan).toContainText("Which segments contributed most?");
    await expect(plan).toContainText("segments: sql analysis by sql analyst; after trend");
    await expect(plan).toContainText("Estimated planning cost: up to $0.0125 under the current enforced provider limits.");
    await expect(plan).toContainText("Prior-year churn is not in the uploaded table.");
    await expect(plan).toContainText("plan: required");
    await expect(plan.locator("time")).toHaveAttribute("datetime", "2026-10-30T12:00:00Z");
    await expectNoHorizontalOverflow(page);

    await pressAndConfirm(page, plan.getByRole("button", { name: "Approve and run plan" }), "Approve and run plan");
    await expect(status(page)).toHaveText("Running");
    // The browser echoes only the exact server binding: no approver, authority, or scope fields.
    expect(api.bodies.plan).toEqual([{ decision: "approve", plan_digest: DIGEST, authority_epoch: 3, follow_up_rounds: 0 }]);
    expect(api.counts.plan).toBe(1);

    const progressSection = page.locator(".investigation-progress");
    await expect(progressSection).toContainText("1 task completed out of 3.");
    await expect(progressSection.locator("progress")).toHaveAttribute("max", "3");
    await expect(progressSection.locator(".investigation-owner-progress")).toContainText("1 completed; 1 running; 1 pending; 3 total");
    await expect(page.getByTestId("operation-announcer")).not.toContainText("SELECT");
    await expectNoHorizontalOverflow(page);
    await expectNoAxeViolations(page);
  });

  test(`${name}: SQL wave approve, reject, and edit are submitted once, with the exact binding`, async ({ page }) => {
    const api = await routeApi(page);
    api.server = awaitingSql();
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    api.wavePost = async (route) => {
      await held;
      api.server = running();
      await route.fulfill({ status: 202, json: api.server });
    };
    await openProposal(page, viewport);

    const heading = page.getByRole("heading", { name: "Review SQL wave 1" });
    await expect(heading).toBeFocused();
    await expect(page.locator(".investigation-wave")).toContainText("The browser clock grants no authority.");
    const submit = page.getByRole("button", { name: "Submit decisions" });
    await expect(submit).toBeDisabled();
    await expectNoAxeViolations(page);

    const statement = (index: number) => page.getByRole("group", { name: `Decision for statement ${index}` });
    await statement(1).getByRole("radio", { name: "Approve as shown" }).check();
    await statement(2).getByRole("radio", { name: "Reject" }).check();
    await statement(3).getByRole("radio", { name: "Edit and revalidate" }).check();
    const editor = page.getByLabel("Edited SQL for statement 3");
    await expect(editor).toHaveValue(SQL_TEXT.regions);
    // An unchanged "edit" is not a decision.
    await expect(submit).toBeDisabled();
    const edited = 'SELECT "region", COUNT(*) FROM "uploaded_data" WHERE "churned" = 1 GROUP BY "region"';
    await editor.fill(edited);
    await expect(page.locator(".investigation-wave-review")).toContainText("1 approve; 1 reject; 1 edit; 0 undecided.");
    await expect(submit).toBeEnabled();
    await expectNoHorizontalOverflow(page);
    await expectNoAxeViolations(page);

    await submit.focus();
    await page.keyboard.press("Enter");
    // SQL leaves the page as soon as the decisions are sent, before the server answers.
    await expect(page.locator(".investigation-wave")).toHaveCount(0);
    await expect(panel(page)).not.toContainText("SELECT");
    expect(api.bodies.wave).toEqual([
      {
        wave_id: WAVE,
        wave_digest: WAVE_DIGEST,
        items: [
          { task_id: "trend", sql_fingerprint: "4".repeat(64), decision: "approve" },
          { task_id: "segments", sql_fingerprint: "5".repeat(64), decision: "reject" },
          { task_id: "regions", sql_fingerprint: "6".repeat(64), decision: "edit", edited_sql: edited },
        ],
      },
    ]);
    release();
    await expect(status(page)).toHaveText("Running");
    expect(api.counts.wave).toBe(1);
    expect(await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage }))).not.toContain("SELECT");
  });
}

test("keyboard-only rejection: Escape sends nothing; a confirmed rejection is terminal", async ({ page }) => {
  const api = await routeApi(page);
  api.plan = (route) => {
    api.server = pendingPlan({ status: "plan_rejected", proposal: null, job_id: null, updated_at: stamp() });
    return route.fulfill({ status: 200, json: api.server });
  };
  await openProposal(page);
  await reachPlanReview(page, api);

  const reject = page.locator(".investigation-plan").getByRole("button", { name: "Reject plan" });
  await reject.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toContainText("cannot be undone");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  expect(api.counts.plan).toBe(0);

  await pressAndConfirm(page, reject, "Reject plan");
  await expect(status(page)).toHaveText("Plan rejected");
  await expect(page.getByRole("heading", { name: "Investigation status" })).toBeFocused();
  expect(api.bodies.plan).toEqual([{ decision: "reject", plan_digest: DIGEST, authority_epoch: 3, follow_up_rounds: 0 }]);
  await expect(page.getByRole("button", { name: /Approve and run plan|Cancel investigation/ })).toHaveCount(0);
  await expectNoAxeViolations(page);
});

test("keyboard-only cancellation: requested is not claimed as cancelled until the server confirms it", async ({ page }) => {
  const api = await routeApi(page);
  api.server = running();
  api.cancel = (route) => route.fulfill({ status: 202, json: api.server });
  await openProposal(page);

  await pressAndConfirm(page, page.getByRole("button", { name: "Cancel investigation" }), "Cancel investigation");
  await expect(panel(page)).toContainText("Cancellation requested.");
  await expect(panel(page)).toContainText("has not yet reached a terminal cancelled state");
  await expect(status(page)).toHaveText("Running");
  await expectNoAxeViolations(page);

  api.server = running({
    status: "cancelled",
    explanation: "This investigation was cancelled. Work already in flight was discarded.",
    explanation_code: "cancelled_by_user",
    updated_at: stamp(),
  });
  await expect(panel(page)).toContainText("Cancellation is confirmed by the server.");
  await expect(status(page)).toHaveText("Cancelled");
  expect(api.counts.cancel).toBe(1);
});

test("cancellation that arrives too late says the investigation finished first", async ({ page }) => {
  const api = await routeApi(page);
  api.server = running();
  api.cancel = (route) => {
    api.server = running({ status: "completed", progress: null, job_id: null, updated_at: stamp() });
    return route.fulfill({ status: 202, json: api.server });
  };
  await openProposal(page);

  await pressAndConfirm(page, page.getByRole("button", { name: "Cancel investigation" }), "Cancel investigation");
  await expect(panel(page)).toContainText("reached completed before cancellation took effect");
  await expect(panel(page)).toContainText("belongs to UI-6c and is not shown here");
  await expect(panel(page)).not.toContainText("Cancellation is confirmed");
});

test("after an API restart the durable record says interrupted and offers no resume", async ({ page }) => {
  const api = await routeApi(page);
  api.server = investigation({
    status: "interrupted",
    plan: PLAN,
    job_id: null,
    explanation:
      "The service restarted before this investigation finished. Nothing resumes automatically; start a new investigation to try again.",
    explanation_code: "interrupted_by_restart",
  });
  await openProposal(page);

  await expect(status(page)).toHaveText("Interrupted");
  await expect(panel(page)).toContainText("Nothing resumes automatically");
  await expect(page.getByRole("button", { name: /Approve|Reject plan|Cancel investigation|Start investigation|Resume/ })).toHaveCount(0);
  await expectNoAxeViolations(page);
});

test("a missing process-local job is an unknown job outcome; the durable lifecycle stays and nothing restarts", async ({ page }) => {
  const api = await routeApi(page);
  api.server = running({ progress: null });
  api.job = (route) => route.fulfill(notFound);
  await openProposal(page);

  await expect(status(page)).toHaveText("Running");
  await expect(panel(page)).toContainText("The linked process-local job is unavailable.");
  await expect(panel(page)).toContainText("Detailed task-owner progress is unavailable. No task percentage or agent activity is estimated.");
  await expect(panel(page).locator("progress")).toHaveCount(0);
  expect(api.counts.create + api.counts.plan + api.counts.wave).toBe(0);
  await expectNoAxeViolations(page);
});

test("a late lookup for an earlier proposal is discarded when a newer proposal arrives", async ({ page }) => {
  const api = await routeApi(page);
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  api.byMessage = async (route) => {
    if (route.request().url().endsWith(`/by-message/${PROPOSAL}`)) {
      await held;
      await route.fulfill({ status: 200, json: pendingPlan() });
    } else {
      await route.fulfill(notFound);
    }
  };
  await openProposal(page);
  await expect(panel(page)).toHaveAttribute("aria-busy", "true");

  api.nextProposal = "msg-proposal-2";
  await askQuestion(page);
  await expect(page.getByRole("button", { name: "Start investigation" })).toBeEnabled();
  release();
  await page.waitForTimeout(500);
  await expect(planHeading(page)).toHaveCount(0);
  await expect(page.getByText("Explain why churn increased last quarter.")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Start investigation" })).toBeEnabled();
});

test("a late lookup from a deleted workspace never reaches the replacement workspace", async ({ page }) => {
  const api = await routeApi(page);
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  api.byMessage = async (route) => {
    await held;
    await route.fulfill({ status: 200, json: pendingPlan() });
  };
  await openProposal(page);
  await expect(panel(page)).toHaveAttribute("aria-busy", "true");

  await page.getByRole("button", { name: "Reset workspace" }).click();
  await page.getByRole("dialog", { name: "Delete this workspace?" }).getByRole("button", { name: "Delete workspace" }).click();
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  release();
  await page.waitForTimeout(500);
  await expect(panel(page)).toHaveCount(0);
  await expect(page.getByText("Explain why churn increased last quarter.")).toHaveCount(0);
});

test("an uncertain plan decision locks every decision until Check server state reads the outcome", async ({ page }) => {
  const api = await routeApi(page);
  api.plan = (route) => route.abort("failed");
  await openProposal(page);
  await reachPlanReview(page, api);

  await pressAndConfirm(page, page.locator(".investigation-plan").getByRole("button", { name: "Approve and run plan" }), "Approve and run plan");
  await expect(panel(page)).toContainText("The server has not confirmed the investigation command.");
  await expect(checkServer(page)).toBeVisible();
  await expect(page.getByRole("button", { name: /Approve and run plan|Reject plan|Cancel investigation/ })).toHaveCount(0);
  await expectNoAxeViolations(page);

  // The server still shows the earlier state, so the outcome stays unknown and nothing is resent.
  await checkServer(page).click();
  await expect(checkServer(page)).toBeVisible();
  expect(api.counts.plan).toBe(1);

  api.server = running();
  await checkServer(page).click();
  await expect(status(page)).toHaveText("Running");
  await expect(checkServer(page)).toHaveCount(0);
  expect(api.counts.plan).toBe(1);
});

test("an uncertain creation is checked read-only; only an explicit later start may send it again", async ({ page }) => {
  const api = await routeApi(page);
  api.create = (route) => route.abort("failed");
  await openProposal(page);

  await page.getByRole("button", { name: "Start investigation" }).click();
  await expect(checkServer(page)).toBeVisible();
  await expect(page.getByRole("button", { name: "Start investigation" })).toHaveCount(0);
  await expectNoAxeViolations(page);

  await checkServer(page).click();
  await expect(page.getByRole("button", { name: "Start investigation" })).toBeEnabled();
  expect(api.counts.create).toBe(1);
});

test("an uncertain SQL-wave submission clears the SQL and waits for Check server state", async ({ page }) => {
  const api = await routeApi(page);
  api.server = awaitingSql();
  api.wavePost = (route) => route.abort("failed");
  await openProposal(page);

  await expect(page.getByRole("heading", { name: "Review SQL wave 1" })).toBeVisible();
  for (let index = 1; index <= TASK_IDS.length; index += 1) {
    await page.getByRole("group", { name: `Decision for statement ${index}` }).getByRole("radio", { name: "Approve as shown" }).check();
  }
  await page.getByRole("button", { name: "Submit decisions" }).click();

  await expect(checkServer(page)).toBeVisible();
  await expect(page.locator(".investigation-wave")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("SELECT");
  await expectNoAxeViolations(page);

  api.server = running();
  await checkServer(page).click();
  await expect(status(page)).toHaveText("Running");
  expect(api.counts.wave).toBe(1);
});

test("a 409 after the SQL-wave deadline shows the server's expired state, not a browser-clock guess", async ({ page }) => {
  const api = await routeApi(page);
  api.server = awaitingSql();
  api.wavePost = (route) => {
    api.server = investigation({
      status: "expired",
      plan: PLAN,
      job_id: null,
      explanation: "This investigation ended without a result.",
      updated_at: stamp(),
    });
    return route.fulfill({
      status: 409,
      json: { error: { code: "sql_wave_not_pending", message: "The SQL wave is no longer pending.", request_id: "req-409" } },
    });
  };
  await openProposal(page);

  await expect(page.getByRole("heading", { name: "Review SQL wave 1" })).toBeVisible();
  for (let index = 1; index <= TASK_IDS.length; index += 1) {
    await page.getByRole("group", { name: `Decision for statement ${index}` }).getByRole("radio", { name: "Reject" }).check();
  }
  await page.getByRole("button", { name: "Submit decisions" }).click();

  await expect(status(page)).toHaveText("Expired");
  await expect(checkServer(page)).toHaveCount(0);
  await expect(page.locator(".investigation-wave")).toHaveCount(0);
  await expectNoAxeViolations(page);
});
