import { expect, test as base } from "@playwright/test";

const CSP_VIOLATION = /Content Security Policy|Refused to (?:load|execute|apply|connect|evaluate)/i;

/**
 * Every browser journey also asserts that the page raised no Content-Security-Policy
 * violation. Against the local dist server this is a no-op; against the nginx image
 * (E2E_BASE_URL) it proves the shipped policy does not break the application or charts.
 */
export const test = base.extend({
  page: async ({ page }, runTest) => {
    // The shipped client asks the API how to authenticate before rendering. Mocked journeys run in
    // local development mode; the real-stack run talks to the real API instead.
    if (!process.env.E2E_REAL_STACK) {
      await page.route("**/api/v1/auth/config", (route) =>
        route.fulfill({ status: 200, json: { mode: "local", login_available: false } }),
      );
    }
    const violations: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error" && CSP_VIOLATION.test(message.text())) {
        violations.push(message.text());
      }
    });
    await runTest(page);
    expect(violations, "Content-Security-Policy violations").toEqual([]);
  },
});

export { expect };
