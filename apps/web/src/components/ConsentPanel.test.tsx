import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiClientError, type Consent } from "../api/client";
import {
  consentCurrentFixture,
  consentMissingFixture,
  consentWithdrawnFixture,
} from "../test/fixtures";
import type { ConsentView } from "../workspace/useConsent";
import { ConsentPanel } from "./ConsentPanel";

const ready = (consent: Consent): ConsentView => ({ phase: "ready", consent, error: null });

function renderPanel(view: ConsentView, overrides: Partial<Parameters<typeof ConsentPanel>[0]> = {}) {
  const handlers = { onAccept: vi.fn(), onWithdraw: vi.fn(), onRetry: vi.fn() };
  const result = render(<ConsentPanel view={view} busy={false} pending={null} {...handlers} {...overrides} />);
  return { ...handlers, ...result };
}

const openAiConsent: Consent = {
  ...consentMissingFixture,
  notice: {
    version: "2026-10-openai-1",
    title: "Hosted model and OpenAI data sharing",
    sections: [{ heading: "What investigations send to OpenAI", body: "Kept for about ten minutes." }],
    recipients: ["Gemini", "OpenAI"],
    digest: "b".repeat(64),
  },
};

describe("ConsentPanel", () => {
  it("renders the complete server notice open, with every recipient, before acceptance", async () => {
    const { onAccept } = renderPanel(ready(openAiConsent));

    expect(screen.getByRole("heading", { level: 3, name: "Hosted model and OpenAI data sharing" })).toBeVisible();
    expect(screen.getByText("Notice version 2026-10-openai-1")).toBeVisible();
    expect(screen.getByRole("heading", { level: 4, name: "External recipients" })).toBeVisible();
    expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual(["Gemini", "OpenAI"]);
    expect(screen.getByRole("heading", { level: 4, name: "What investigations send to OpenAI" })).toBeVisible();
    expect(screen.getByText("Kept for about ten minutes.")).toBeVisible();

    await userEvent.setup().click(screen.getByRole("button", { name: "Accept this notice for Gemini and OpenAI" }));
    expect(onAccept).toHaveBeenCalledWith(openAiConsent.notice);
  });

  it("has no prechecked or hidden acceptance control", () => {
    renderPanel(ready(consentMissingFixture));
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Accept this notice for Gemini",
    ]);
  });

  it("explains stale consent, names what it still covers, and offers both actions", () => {
    renderPanel(
      ready({
        ...openAiConsent,
        accepted: true,
        status: "stale",
        questions_enabled: true,
        notice_version: "2026-09",
        accepted_at: "2026-09-29T12:01:00Z",
        data_recipients: ["Gemini"],
      }),
    );
    expect(screen.getByText("Needs a new acceptance")).toBeVisible();
    expect(screen.getByText(/authorizes only Gemini, which does not cover the current notice/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Accept this notice for Gemini and OpenAI" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Withdraw consent" })).toBeEnabled();
  });

  it("explains withdrawal without claiming recall", () => {
    renderPanel(ready(consentWithdrawnFixture));
    expect(screen.getByText("Withdrawn")).toBeVisible();
    expect(screen.getByText(/Requests sent before the withdrawal were not recalled/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Withdraw consent" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept this notice for Gemini" })).toBeEnabled();
  });

  it("keeps the accepted notice readable by keyboard, and offers withdrawal", async () => {
    const user = userEvent.setup();
    const { onWithdraw } = renderPanel(ready(consentCurrentFixture));
    expect(screen.getByText("Accepted")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Accept this notice/ })).not.toBeInTheDocument();

    await user.tab();
    expect(screen.getByText("Read the current notice")).toHaveFocus();
    // jsdom does not toggle <details> on Enter; the real-stack journey covers that key in a browser.
    await user.click(screen.getByText("Read the current notice"));
    expect(screen.getByText("Withdrawal does not recall a request already sent.")).toBeVisible();

    await user.tab();
    expect(screen.getByRole("button", { name: "Withdraw consent" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onWithdraw).toHaveBeenCalledOnce();
  });

  it("disables both actions and says what is pending, without claiming an outcome", () => {
    renderPanel(ready({ ...consentCurrentFixture, status: "stale" }), { busy: true, pending: "withdraw" });
    const buttons = screen.getAllByRole("button");
    expect(buttons.every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
    expect(screen.getByRole("button", { name: "Withdrawing…" })).toBeVisible();
    expect(screen.getByRole("region")).toHaveAttribute("aria-busy", "true");
  });

  it("shows no notice and no action while loading", () => {
    renderPanel({ phase: "loading", consent: null, error: null });
    const region = screen.getByRole("region");
    expect(region).toHaveAttribute("aria-busy", "true");
    expect(within(region).queryByRole("button")).not.toBeInTheDocument();
  });

  it("offers a retry, never an acceptance, when the state cannot be read", async () => {
    const { onRetry } = renderPanel({
      phase: "error",
      consent: null,
      error: new ApiClientError("Server unavailable.", "network_error"),
    });
    expect(screen.getByText(/Server unavailable\./)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Accept/ })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Read consent again" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("renders nothing when this server shares data with no recipient", () => {
    const { container } = renderPanel(
      ready({ ...consentMissingFixture, required: false, status: "not_required", questions_enabled: true, notice: null }),
    );
    expect(container).toBeEmptyDOMElement();
  });
});
