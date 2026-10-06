import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Workspace } from "../api/client";
import { ConversationPanel } from "./ConversationPanel";
import { workspaceFixture } from "../test/fixtures";

function renderPanel(workspace: Workspace) {
  return render(
    <ConversationPanel
      workspace={workspace}
      messages={[]}
      busy={false}
      hasContext
      onAcceptConsent={vi.fn()}
      onAsk={vi.fn()}
      onApproval={vi.fn()}
      onExport={vi.fn()}
    />,
  );
}

describe("consent notice", () => {
  it("names every provider that will receive data", () => {
    renderPanel({ ...workspaceFixture, data_recipients: ["Gemini", "Anthropic"] });

    expect(screen.getByRole("status")).toHaveTextContent("to Gemini and Anthropic");
    expect(
      screen.getByRole("button", { name: "I understand, enable Gemini and Anthropic" }),
    ).toBeInTheDocument();
  });

  it("defaults to Gemini when the API does not list recipients", () => {
    renderPanel({ ...workspaceFixture });

    expect(screen.getByRole("button", { name: "I understand, enable Gemini" })).toBeInTheDocument();
  });

  it("shows no consent prompt once the notice is accepted", () => {
    renderPanel({ ...workspaceFixture, consent_accepted: true, data_recipients: ["Anthropic"] });

    expect(screen.queryByRole("button", { name: /I understand/ })).not.toBeInTheDocument();
  });
});
