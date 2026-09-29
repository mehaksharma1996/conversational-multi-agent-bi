import { expect, test as base } from "@playwright/test";

const CSP_VIOLATION = /Content Security Policy|Refused to (?:load|execute|apply|connect|evaluate)/i;

/**
 * Every browser journey also asserts that the page raised no Content-Security-Policy
 * violation. Against the local dist server this is a no-op; against the nginx image
 * (E2E_BASE_URL) it proves the shipped policy does not break the application or charts.
 */
export const test = base.extend({
  page: async ({ page }, runTest) => {
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
