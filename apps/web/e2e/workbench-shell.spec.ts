import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import {
  conversationFixture,
  documentCollectionFixture,
  messageFixture,
  workspaceFixture,
} from "./fixtures";
import { expect, test } from "./test";

const PHONE = { width: 375, height: 812 };

const citedMessage = {
  ...messageFixture,
  answer: "Transactions over 500 require review (Source 1).",
  sources: [{ citation: "Source 1 - policy.pdf, page 2" }],
};

async function routeApi(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/config") {
      await route.fallback();
    } else if (request.method() === "GET" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 200, json: { items: [] } });
    } else if (request.method() === "POST" && path === "/api/v1/workspaces") {
      await route.fulfill({ status: 201, json: { ...workspaceFixture, consent_accepted: true } });
    } else if (request.method() === "GET" && path.endsWith("/export")) {
      await route.fulfill({
        status: 200,
        contentType: "application/zip",
        body: Buffer.from("PK workspace export"),
      });
    } else if (request.method() === "POST" && path.endsWith("/document-collections")) {
      await route.fulfill({ status: 201, json: documentCollectionFixture });
    } else if (request.method() === "POST" && path.endsWith("/conversations")) {
      await route.fulfill({ status: 201, json: conversationFixture });
    } else if (request.method() === "POST" && path.endsWith("/messages")) {
      await route.fulfill({ status: 201, json: citedMessage });
    } else {
      await route.abort("failed");
    }
  });
}

/** True when keyboard focus is inside the workbench navigation. */
function focusInNavigation(page: Page) {
  return page.evaluate(
    'document.getElementById("workbench-navigation").contains(document.activeElement)',
  );
}

test.describe("phone navigation drawer", () => {
  test.beforeEach(async ({ page }) => {
    await routeApi(page);
    await page.setViewportSize(PHONE);
    await page.goto("/");
    await expect(page.getByText("Local workspace ready")).toBeVisible();
  });

  test("closed drawer is skipped by Tab and hidden from assistive technology", async ({ page }) => {
    const navigation = page.locator("#workbench-navigation");
    await expect(navigation).toHaveAttribute("inert", "");
    await expect(page.getByRole("navigation", { name: "Primary" })).toHaveCount(0);

    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "Open navigation" })).toBeFocused();
    for (let step = 0; step < 8; step += 1) {
      await page.keyboard.press("Tab");
      expect(await focusInNavigation(page)).toBe(false);
    }
  });

  test("keyboard opens, contains, and dismisses the modal drawer", async ({ page }) => {
    const toggle = page.getByRole("button", { name: "Open navigation" });
    await toggle.focus();
    await page.keyboard.press("Enter");

    const drawer = page.getByRole("dialog", { name: "Workbench navigation" });
    await expect(drawer).toBeVisible();
    await expect(drawer).toHaveAttribute("aria-modal", "true");
    await expect(drawer.getByRole("link", { name: /Overview/ })).toBeFocused();
    await expect(page.locator(".workbench-frame")).toHaveAttribute("inert", "");
    // The toggle now names the action it would perform.
    await expect(page.locator(".mobile-nav-toggle")).toHaveAccessibleName("Close navigation");
    await expect(page.locator(".mobile-nav-toggle")).toHaveAttribute("aria-expanded", "true");
    await expect.poll(async () => (await new AxeBuilder({ page }).analyze()).violations).toEqual([]);

    // Focus cycles inside the drawer in both directions and never reaches the obscured page.
    for (let step = 0; step < 10; step += 1) {
      await page.keyboard.press("Tab");
      expect(await focusInNavigation(page)).toBe(true);
    }
    for (let step = 0; step < 10; step += 1) {
      await page.keyboard.press("Shift+Tab");
      expect(await focusInNavigation(page)).toBe(true);
    }

    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);
    await expect(toggle).toBeFocused();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(page.locator("#workbench-navigation")).toHaveAttribute("inert", "");

    // The labelled close button inside the drawer also returns focus to the toggle.
    await page.keyboard.press("Enter");
    await expect(drawer.getByRole("link", { name: /Overview/ })).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(drawer.getByRole("button", { name: "Close navigation" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(drawer).toHaveCount(0);
    await expect(toggle).toBeFocused();
  });

  test("keyboard selection navigates, closes the drawer, and focuses the section", async ({ page }) => {
    await page.getByRole("button", { name: "Open navigation" }).focus();
    await page.keyboard.press("Enter");
    const drawer = page.getByRole("dialog", { name: "Workbench navigation" });
    await expect(drawer.getByRole("link", { name: /Overview/ })).toBeFocused();

    for (let step = 0; step < 4; step += 1) await page.keyboard.press("Tab");
    await expect(drawer.getByRole("link", { name: /Ask/ })).toBeFocused();
    await page.keyboard.press("Enter");

    await expect(page).toHaveURL(/#ask$/);
    await expect(drawer).toHaveCount(0);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Ask across");
    await expect(page.locator("[data-section-heading]")).toBeFocused();
  });

  test("viewport changes never leave the drawer modal or focus hidden", async ({ page }) => {
    await page.getByRole("button", { name: "Open navigation" }).click();
    await expect(page.getByRole("dialog", { name: "Workbench navigation" })).toBeVisible();

    await page.setViewportSize({ width: 1280, height: 800 });
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator(".workbench-frame")).not.toHaveAttribute("inert", "");
    await expect(page.getByRole("complementary", { name: "Workbench navigation" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Collapse navigation" })).toBeVisible();

    const dataLink = page.getByRole("link", { name: /Data/ });
    await dataLink.focus();
    await expect(dataLink).toBeFocused();
    await page.setViewportSize(PHONE);
    await expect(page.locator("#workbench-navigation")).toHaveAttribute("inert", "");
    await expect(page.getByRole("button", { name: "Open navigation" })).toBeFocused();
  });

  test("workspace export stays available and works from the keyboard", async ({ page }) => {
    const exportButton = page.getByRole("button", { name: "Export workspace" });
    await expect(exportButton).toBeVisible();
    await expect(exportButton).toHaveText("Export", { useInnerText: true });

    // Skip link, navigation toggle, then the export action, ahead of reset.
    for (let step = 0; step < 3; step += 1) await page.keyboard.press("Tab");
    await expect(exportButton).toBeFocused();

    const exportRequest = page.waitForRequest(
      (request) => request.method() === "GET" && request.url().endsWith("/workspaces/workspace-1/export"),
    );
    const download = page.waitForEvent("download");
    await page.keyboard.press("Enter");
    await exportRequest;
    expect((await download).suggestedFilename()).toBe("workspace-workspace-1.zip");
  });
});

test("citations reveal evidence without disturbing section history", async ({ page }) => {
  await routeApi(page);
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

  // 1. Navigate to Ask and get a cited answer.
  await page.getByRole("link", { name: /Ask/ }).click();
  await expect(page).toHaveURL(/#ask$/);
  await page.getByLabel("Business question").fill(citedMessage.question);
  await page.getByRole("button", { name: "Ask workbench" }).click();
  const citation = page.getByRole("link", { name: "Source 1" });
  await expect(citation).toBeVisible();

  // 2. Activate the citation from the keyboard: the passage is revealed and focused, and neither
  // the URL nor the history changes.
  const source = page.locator("#source-message-1-1");
  await expect(source).toBeHidden();
  const historyLength = await page.evaluate("window.history.length");
  await citation.focus();
  await page.keyboard.press("Enter");
  await expect(source).toBeVisible();
  await expect(source).toBeFocused();
  await expect(source).toContainText("policy.pdf");
  await expect(page).toHaveURL(/#ask$/);
  expect(await page.evaluate("window.history.length")).toBe(historyLength);

  // 3. Navigate to Data.
  await page.getByRole("link", { name: /Data/ }).click();
  await expect(page).toHaveURL(/#data$/);

  // 4-5. Back restores Ask, and the citation still works.
  await page.goBack();
  await expect(page).toHaveURL(/#ask$/);
  await expect(page.getByRole("link", { name: /Ask/ })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Ask across");
  await page.getByRole("link", { name: "Source 1" }).click();
  await expect(source).toBeFocused();
  await expect(page).toHaveURL(/#ask$/);

  // 6-7. Forward restores Data.
  await page.goForward();
  await expect(page).toHaveURL(/#data$/);
  await expect(page.getByRole("link", { name: /Data/ })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Prepare a trusted dataset");

  // 8. Reload keeps the supported deep link.
  await page.reload();
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await expect(page).toHaveURL(/#data$/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Prepare a trusted dataset");
  await expect(page.getByRole("link", { name: /Data/ })).toHaveAttribute("aria-current", "page");
});

test("a citation fragment from an older link opens Ask deterministically", async ({ page }) => {
  await routeApi(page);
  await page.goto("/#source-message-1-1");
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await expect(page.getByRole("link", { name: /Ask/ })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Ask across");
});
