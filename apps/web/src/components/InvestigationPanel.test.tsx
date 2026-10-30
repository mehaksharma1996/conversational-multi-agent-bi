import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Investigation } from "../api/client";
import type { InvestigationAvailability } from "../investigations/useInvestigation";
import { investigationProposalMessageFixture } from "../test/fixtures";
import { InvestigationPanel } from "./InvestigationPanel";

const proposal = investigationProposalMessageFixture.proposal;
const callbacks = {
  onStart: vi.fn(),
  onApprove: vi.fn(),
  onReject: vi.fn(),
  onCancel: vi.fn(),
  onCheckServerState: vi.fn(),
};

function pendingPlan(overrides: Partial<Investigation> = {}): Investigation {
  return {
    id: "investigation-1",
    conversation_id: "conversation-1",
    message_id: investigationProposalMessageFixture.id,
    status: "awaiting_plan_approval",
    created_at: "2026-10-30T10:00:00Z",
    updated_at: "2026-10-30T10:01:00Z",
    plan: {
      schema_version: 2,
      objective: "Explain the churn increase",
      sub_questions: ["Which segment changed most?"],
      tasks: [
        {
          id: "task-1",
          kind: "sql_analysis",
          assignee: "business_investigator",
          depends_on: [],
          success_criterion: "Segment changes are quantified",
        },
      ],
      hypotheses: [
        {
          id: "hypothesis-1",
          statement: "Enterprise churn increased",
          testability: "observational",
          tests: ["task-1"],
        },
      ],
    },
    plan_digest: "a".repeat(64),
    authority_epoch: 1,
    proposal: {
      cost_estimate: {
        kind: "estimate",
        basis: "token_limits_x_highest_eligible_price",
        bound: "enforced_upper_bound",
        amount_usd: 0.12,
        limitations: [],
      },
      missing_information: ["Historical contract changes"],
      approval_requirements: [{ kind: "plan", status: "required" }],
      plan_expires_at: "2026-10-31T10:00:00Z",
    },
    ...overrides,
  };
}

function props(
  investigation: Investigation | null,
  availability: InvestigationAvailability = "loaded",
) {
  return {
    proposal,
    availability,
    investigation,
    sqlWave: null,
    command: null,
    unconfirmed: null,
    error: null,
    job: { state: "idle" } as const,
    busy: false,
    ...callbacks,
  };
}

describe("InvestigationPanel", () => {
  it("requires an explicit start and describes its limited authority", () => {
    render(<InvestigationPanel {...props(null, "available")} />);

    expect(screen.getByText(/authorizes planning only/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start investigation" }));
    expect(callbacks.onStart).toHaveBeenCalledOnce();
  });

  it("renders the exact plan review, planning cost, deadline, and decision actions", () => {
    render(<InvestigationPanel {...props(pendingPlan())} />);

    expect(screen.getByRole("heading", { name: "Review the proposed investigation plan" })).toBeInTheDocument();
    expect(screen.getByText("Explain the churn increase")).toBeInTheDocument();
    expect(screen.getByText("Which segment changed most?")).toBeInTheDocument();
    expect(screen.getByText((_, element) => element?.tagName === "LI" && /task-1.*sql analysis.*business investigator/i.test(element.textContent ?? ""))).toBeInTheDocument();
    expect(screen.getByText((_, element) => element?.tagName === "LI" && /hypothesis-1.*Enterprise churn increased/i.test(element.textContent ?? ""))).toBeInTheDocument();
    expect(screen.getByText(/Estimated planning cost: up to \$0\.12/i)).toBeInTheDocument();
    expect(screen.getByText("Historical contract changes")).toBeInTheDocument();
    expect(screen.getByText("plan: required")).toBeInTheDocument();
    expect(document.querySelector("time")).toHaveAttribute("datetime", "2026-10-31T10:00:00Z");

    fireEvent.click(screen.getByRole("button", { name: "Approve and run plan" }));
    fireEvent.click(screen.getByRole("button", { name: "Reject plan" }));
    expect(callbacks.onApprove).toHaveBeenCalledOnce();
    expect(callbacks.onReject).toHaveBeenCalledOnce();
  });

  it("fails closed when proposal review details are unavailable", () => {
    render(<InvestigationPanel {...props(pendingPlan({ proposal: null }))} />);

    expect(screen.getByText(/cannot be approved from the browser/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve and run plan" })).toBeDisabled();
  });

  it.each([
    ["indicative", "Indicative planning-cost estimate: $0.12. This is not a maximum."],
    ["unavailable", "A planning-cost estimate is unavailable"],
  ] as const)("renders %s cost semantics without promoting it to authority", (bound, wording) => {
    const current = pendingPlan();
    render(
      <InvestigationPanel
        {...props({
          ...current,
          proposal: {
            ...current.proposal!,
            cost_estimate: {
              ...current.proposal!.cost_estimate,
              bound,
              amount_usd: bound === "unavailable" ? null : 0.12,
              limitations: bound === "unavailable" ? ["pricing_unknown"] : [],
            },
          },
        })}
      />,
    );
    expect(screen.getByText(new RegExp(wording.replace(/[.$]/g, "\\$&"), "i"))).toBeInTheDocument();
  });

  it("moves focus from planning to the newly available plan review", () => {
    const planning = pendingPlan({ status: "planning", plan: null, proposal: null });
    const view = render(<InvestigationPanel {...props(planning)} />);

    view.rerender(<InvestigationPanel {...props(pendingPlan())} />);

    expect(screen.getByRole("heading", { name: "Review the proposed investigation plan" })).toHaveFocus();
  });

  it("shows an interrupted explanation without offering an automatic resume", () => {
    render(
      <InvestigationPanel
        {...props(pendingPlan({
          status: "interrupted",
          explanation: "The API restarted. Nothing resumes automatically.",
        }))}
      />,
    );

    expect(screen.getByText("The API restarted. Nothing resumes automatically.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resume/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel investigation" })).not.toBeInTheDocument();
  });
});
