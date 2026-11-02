import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Message, Workspace } from "../api/client";
import { ConversationPanel } from "./ConversationPanel";
import type { ConsentGate } from "../workspace/useConsent";
import { investigationProposalMessageFixture, messageFixture, workspaceFixture } from "../test/fixtures";

function panel(workspace: Workspace, messages: Message[] = [], consentGate: ConsentGate = "open") {
  return (
    <ConversationPanel
      workspace={workspace}
      consentGate={consentGate}
      messages={messages}
      busy={false}
      hasContext
      question=""
      requireSqlApproval={false}
      approvalDrafts={{}}
      onQuestionChange={vi.fn()}
      onRequireSqlApprovalChange={vi.fn()}
      onApprovalDraftChange={vi.fn()}
      onAsk={vi.fn()}
      onApproval={vi.fn()}
      onExport={vi.fn()}
    />
  );
}

function renderPanel(workspace: Workspace, messages: Message[] = []) {
  return render(panel(workspace, messages));
}

describe("consent gate", () => {
  const BLOCKED = "Accept the data-sharing notice above to enable questions and investigations.";
  const composer = () => screen.getByLabelText("Business question");

  it("locks the composer until the notice is accepted", () => {
    render(panel(workspaceFixture, [], "needs_acceptance"));
    expect(screen.getByText(BLOCKED)).toBeInTheDocument();
    expect(composer()).toBeDisabled();
  });

  it("unlocks only when the gate is open", () => {
    render(panel(workspaceFixture, [], "open"));
    expect(screen.queryByText(BLOCKED)).not.toBeInTheDocument();
    expect(composer()).toBeEnabled();
  });

  it("stays locked while consent is loading, even when the workspace flag says accepted", () => {
    render(panel({ ...workspaceFixture, consent_accepted: true }, [], "loading"));
    expect(composer()).toBeDisabled();
    expect(screen.getByText(/Checking this workspace's data-sharing consent/)).toBeInTheDocument();
  });

  it("stays locked and says so when consent cannot be verified", () => {
    render(panel({ ...workspaceFixture, consent_accepted: true }, [], "error"));
    expect(composer()).toBeDisabled();
    expect(screen.getByText(/could not be verified/)).toBeInTheDocument();
  });

  it("sends a caller who cannot accept to an administrator", () => {
    render(panel(workspaceFixture, [], "needs_administrator"));
    expect(composer()).toBeDisabled();
    expect(screen.getByText(/A workspace administrator must accept/)).toBeInTheDocument();
  });
});

describe("announcements", () => {
  it("has no live region of its own; outcomes go through the shell's single announcer", () => {
    const workspace = { ...workspaceFixture, consent_accepted: true };
    const view = renderPanel(workspace);

    view.rerender(panel(workspace, [investigationProposalMessageFixture]));

    expect(view.container.querySelector("[aria-live]")).toBeNull();
    expect(screen.getByRole("heading", { name: "Multi-step investigation proposed" })).toBeInTheDocument();
  });
});

describe("history", () => {
  const ready = { ...workspaceFixture, consent_accepted: true };

  it("is an ordered list in server order, with the client-only entry last", () => {
    render(
      <ConversationPanel
        {...panel(ready, [
          { ...messageFixture, id: "m1", question: "First?" },
          { ...messageFixture, id: "m2", question: "Second?" },
        ]).props}
        localEntry={{ kind: "sending", question: "Third?" }}
      />,
    );
    const list = screen.getByRole("list", { name: "Conversation history" });
    const items = Array.from(list.children);
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("First?");
    expect(items[1]).toHaveTextContent("Second?");
    expect(items[2]).toHaveTextContent("Third?");
    expect(screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual([
      "Question 1 of 2",
      "Question 2 of 2",
    ]);
  });

  it("says plainly when there are no questions yet", () => {
    renderPanel(ready);
    expect(screen.queryByRole("list", { name: "Conversation history" })).not.toBeInTheDocument();
    expect(screen.getByText(/No questions yet/)).toBeInTheDocument();
  });
});

describe("composer", () => {
  const ready = { ...workspaceFixture, consent_accepted: true };

  it("states why it is locked, not only in the placeholder", () => {
    render(
      <ConversationPanel
        {...panel(ready, [{ ...messageFixture, status: "pending_approval" }]).props}
      />,
    );
    expect(screen.getByLabelText("Business question")).toBeDisabled();
    expect(screen.getByLabelText("Business question")).toHaveAccessibleDescription(
      /Resolve the pending SQL approval before asking another question/,
    );
  });

  it("enforces the server's 4,000-character limit and shows a counter near it", () => {
    render(<ConversationPanel {...panel(ready).props} question={"x".repeat(3_700)} />);
    const box = screen.getByLabelText("Business question");
    expect(box).toHaveAttribute("maxLength", "4000");
    expect(box).toHaveAccessibleDescription(/3,700 of 4,000 characters/);
  });

  it("pauses asking while an earlier question's outcome is unconfirmed, and offers a server check", () => {
    const recheck = vi.fn();
    render(
      <ConversationPanel {...panel(ready).props} question="Total?" questionUnconfirmed onRecheckQuestion={recheck} />,
    );
    expect(screen.getByLabelText("Business question")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ask workbench" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Check server state" }));
    expect(recheck).toHaveBeenCalledTimes(1);
  });

  it("shows the question being sent, and a failed one as not saved", () => {
    const view = render(
      <ConversationPanel {...panel(ready).props} localEntry={{ kind: "sending", question: "Total?" }} busy asking />,
    );
    expect(screen.getByRole("article", { name: "Answer in progress" })).toHaveAttribute("aria-busy", "true");

    view.rerender(
      <ConversationPanel
        {...panel(ready).props}
        localEntry={{ kind: "failed", question: "Total?", reason: "The provider failed.", requestId: "req-9" }}
      />,
    );
    const failed = screen.getByRole("article", { name: "Question failed" });
    expect(failed).toHaveTextContent("Failed, not saved");
    expect(failed).toHaveTextContent("The provider failed.");
    expect(failed).toHaveTextContent("Request ID: req-9");
    expect(screen.queryByText(/Ask about the analyzed table/)).not.toBeInTheDocument();
  });
});
