import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { workspaceFixture } from "./fixtures";
import { expect, test } from "./test";

const unauthenticated = {
  status: 401,
  json: {
    error: {
      code: "authentication_required",
      message: "Valid authentication credentials are required.",
      request_id: "req-auth",
      details: [],
    },
  },
};

async function routeOidc(page: Page, options: { signedIn: boolean }) {
  await page.route("**/api/v1/auth/config", (route) =>
    route.fulfill({ status: 200, json: { mode: "oidc", login_available: true } }),
  );
  await page.route("**/api/v1/auth/session", (route) =>
    options.signedIn
      ? route.fulfill({
          status: 200,
          json: {
            csrf_token: "e2e-csrf-token",
            roles: ["analyst"],
            expires_at: "2099-01-01T00:00:00Z",
          },
        })
      : route.fulfill(unauthenticated),
  );
}

test("signed-out visitors see only a sign-in screen with no accessibility violations", async ({
  page,
}) => {
  await routeOidc(page, { signedIn: false });
  await page.route("**/api/v1/workspaces", (route) => route.abort("failed"));

  await page.goto("/");

  await expect(page.getByRole("link", { name: "Sign in" })).toHaveAttribute(
    "href",
    "/api/v1/auth/login",
  );
  await expect(page.getByRole("heading", { name: "Turn uploaded data" })).toHaveCount(0);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test("signing out sends the CSRF token and returns to the sign-in screen", async ({ page }) => {
  await routeOidc(page, { signedIn: true });
  await page.route("**/api/v1/workspaces", (route) =>
    route.fulfill({ status: 201, json: workspaceFixture }),
  );
  let logoutHeaders: Record<string, string> = {};
  await page.route("**/api/v1/auth/logout", (route) => {
    logoutHeaders = route.request().headers();
    return route.fulfill({ status: 204 });
  });

  await page.goto("/");
  await expect(page.getByText("Local workspace ready")).toBeVisible();
  await page.getByRole("button", { name: "Sign out" }).click();

  await expect(page.getByText("You have been signed out.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
  expect(logoutHeaders["x-csrf-token"]).toBe("e2e-csrf-token");
  const { origins } = await page.context().storageState();
  expect(origins.flatMap((origin) => origin.localStorage)).toEqual([]);
});

test("an expired session returns the user to sign-in instead of a broken workspace", async ({
  page,
}) => {
  await routeOidc(page, { signedIn: true });
  await page.route("**/api/v1/workspaces", (route) => route.fulfill(unauthenticated));

  await page.goto("/");

  await expect(page.getByText(/session has expired/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
});

test("a failed sign-in redirect shows a fixed message and is removed from the URL", async ({
  page,
}) => {
  await routeOidc(page, { signedIn: false });

  await page.goto("/?auth_error=login_failed");

  await expect(page.getByText(/Sign-in could not be completed/)).toBeVisible();
  expect(new URL(page.url()).search).toBe("");
});
