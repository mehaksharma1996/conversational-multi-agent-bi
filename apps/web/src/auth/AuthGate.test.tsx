import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiClientError,
  endBrowserSession,
  getAuthConfig,
  getBrowserSession,
  setCsrfToken,
  setUnauthorizedHandler,
} from "../api/client";
import { AuthGate } from "./AuthGate";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    getAuthConfig: vi.fn(),
    getBrowserSession: vi.fn(),
    endBrowserSession: vi.fn(),
    setCsrfToken: vi.fn(),
    setUnauthorizedHandler: vi.fn(),
  };
});

const session = {
  csrf_token: "csrf-secret-value",
  roles: ["analyst"],
  expires_at: "2026-10-06T12:00:00Z",
};

function renderGate() {
  return render(
    <AuthGate>
      <div>protected application</div>
    </AuthGate>,
  );
}

function unauthorizedHandler(): () => void {
  const calls = vi.mocked(setUnauthorizedHandler).mock.calls;
  const handler = calls.map((call) => call[0]).filter(Boolean).at(-1);
  if (!handler) throw new Error("unauthorized handler was not registered");
  return handler;
}

describe("AuthGate", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    vi.mocked(getAuthConfig).mockResolvedValue({ mode: "oidc", login_available: true });
    vi.mocked(getBrowserSession).mockResolvedValue(null);
    vi.mocked(endBrowserSession).mockResolvedValue();
  });

  afterEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("renders the application without sign-in controls in local development mode", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue({ mode: "local", login_available: false });

    renderGate();

    expect(await screen.findByText("protected application")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(getBrowserSession).not.toHaveBeenCalled();
  });

  it("shows only a sign-in link, not the application, when signed out", async () => {
    renderGate();

    const link = await screen.findByRole("link", { name: "Sign in" });
    expect(link).toHaveAttribute("href", "/api/v1/auth/login");
    expect(screen.queryByText("protected application")).not.toBeInTheDocument();
  });

  it("explains an unconfigured deployment instead of offering a dead sign-in link", async () => {
    vi.mocked(getAuthConfig).mockResolvedValue({ mode: "oidc", login_available: false });

    renderGate();

    expect(await screen.findByText(/Sign-in is not configured/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Sign in" })).not.toBeInTheDocument();
  });

  it("keeps the CSRF token in memory only and shows the application once signed in", async () => {
    vi.mocked(getBrowserSession).mockResolvedValue(session);

    renderGate();

    expect(await screen.findByText("protected application")).toBeInTheDocument();
    expect(setCsrfToken).toHaveBeenCalledWith("csrf-secret-value");
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(document.body.textContent).not.toContain("csrf-secret-value");
    expect(window.location.href).not.toContain("csrf-secret-value");
  });

  it("signs out, clears the CSRF token, and removes the application", async () => {
    vi.mocked(getBrowserSession).mockResolvedValue(session);
    renderGate();

    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(endBrowserSession).toHaveBeenCalledOnce();
    expect(await screen.findByText("You have been signed out.")).toBeInTheDocument();
    expect(setCsrfToken).toHaveBeenLastCalledWith(null);
    expect(screen.queryByText("protected application")).not.toBeInTheDocument();
  });

  it("stays signed in and reports a failed sign-out with its request ID", async () => {
    vi.mocked(getBrowserSession).mockResolvedValue(session);
    vi.mocked(endBrowserSession).mockRejectedValue(
      new ApiClientError("The request could not be verified.", "csrf_failed", "req-123"),
    );
    renderGate();

    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("could not be verified");
    expect(screen.getByText(/req-123/)).toBeInTheDocument();
    expect(screen.getByText("protected application")).toBeInTheDocument();
  });

  it("returns to sign-in with an expiry notice when the API rejects the session", async () => {
    vi.mocked(getBrowserSession).mockResolvedValue(session);
    renderGate();
    await screen.findByText("protected application");

    act(() => unauthorizedHandler()());

    expect(await screen.findByText(/session has expired/)).toBeInTheDocument();
    expect(screen.queryByText("protected application")).not.toBeInTheDocument();
    expect(setCsrfToken).toHaveBeenLastCalledWith(null);
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("ignores an unauthorized signal while already signed out", async () => {
    renderGate();
    await screen.findByRole("link", { name: "Sign in" });

    act(() => unauthorizedHandler()());

    expect(screen.queryByText(/session has expired/)).not.toBeInTheDocument();
  });

  it("shows a fixed message for a known auth_error and removes it from the URL", async () => {
    window.history.replaceState(null, "", "/?auth_error=login_failed&keep=1#section");

    renderGate();

    expect(await screen.findByText(/Sign-in could not be completed/)).toBeInTheDocument();
    expect(window.location.search).toBe("?keep=1");
    expect(window.location.hash).toBe("#section");
  });

  it.each(["<img src=x onerror=alert(1)>", "constructor", "__proto__", "access_denied"])(
    "never reflects the unknown auth_error value %s",
    async (value) => {
      window.history.replaceState(null, "", `/?auth_error=${encodeURIComponent(value)}`);

      renderGate();

      await screen.findByRole("link", { name: "Sign in" });
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
      expect(document.body.innerHTML).not.toContain(value);
      expect(window.location.search).toBe("");
    },
  );

  it("offers a retry when sign-in status cannot be determined", async () => {
    vi.mocked(getAuthConfig).mockRejectedValueOnce(
      new ApiClientError("The API request failed with status 503.", "http_error", "req-9"),
    );

    renderGate();

    expect(await screen.findByRole("alert")).toHaveTextContent("status 503");
    expect(screen.queryByText("protected application")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("never renders the application while sign-in status is unknown", async () => {
    let release: (value: { mode: "oidc"; login_available: boolean }) => void = () => {};
    vi.mocked(getAuthConfig).mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );

    renderGate();

    expect(screen.getByText(/Checking sign-in status/)).toBeInTheDocument();
    expect(screen.queryByText("protected application")).not.toBeInTheDocument();
    release({ mode: "oidc", login_available: true });
    await waitFor(() => expect(screen.getByRole("link", { name: "Sign in" })).toBeVisible());
  });
});
