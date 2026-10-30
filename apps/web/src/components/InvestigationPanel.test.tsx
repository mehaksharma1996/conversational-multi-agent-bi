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
  onRefreshJob: vi.fn(),
  onSubmitSqlWave: vi.fn(),
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
    cancellation: null,
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

  it("renders only server-recorded task and approved-owner progress", () => {
    render(
      <InvestigationPanel
        {...props(pendingPlan({
          status: "running",
          proposal: null,
          progress: {
            completed_tasks: 1,
            total_tasks: 2,
            task_owners: [
              {
                assignee: "business_investigator",
                completed_tasks: 1,
                running_tasks: 1,
                pending_tasks: 0,
                total_tasks: 2,
              },
            ],
            tasks: [
              {
                task_id: "task-1",
                kind: "sql_analysis",
                assignee: "business_investigator",
                status: "succeeded",
                attempts: 1,
              },
              {
                task_id: "task-2",
                kind: "hypothesis_test",
                assignee: "business_investigator",
                status: "running",
                attempts: 2,
              },
            ],
          },
        }))}
      />,
    );

    expect(screen.getByRole("heading", { name: "Task-owner progress" })).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Investigation tasks completed" })).toHaveValue(1);
    expect(screen.getByText(/1 completed; 1 running; 0 pending; 2 total/i)).toBeInTheDocument();
    expect(screen.getByText("task-2")).toBeInTheDocument();
    expect(screen.getByText("2 attempts")).toBeInTheDocument();
    expect(screen.queryByText(/supervisor activity/i)).not.toBeInTheDocument();
  });

  it("does not synthesize progress when the guarded snapshot is absent", () => {
    render(<InvestigationPanel {...props(pendingPlan({ status: "running", proposal: null, progress: null }))} />);

    expect(screen.getByText(/No task percentage or agent activity is estimated/i)).toBeInTheDocument();
    expect(screen.queryByRole("progressbar", { name: "Investigation tasks completed" })).not.toBeInTheDocument();
  });

  it("distinguishes requested, confirmed, and too-late cancellation outcomes", () => {
    const running = pendingPlan({ status: "running", proposal: null });
    const view = render(<InvestigationPanel {...props(running)} cancellation="requested" />);
    expect(screen.getByText(/has not yet reached a terminal cancelled state/i)).toBeInTheDocument();
    expect(view.container.querySelector("[aria-live], [role='status'], [role='alert']")).toBeNull();

    view.rerender(
      <InvestigationPanel
        {...props({ ...running, status: "cancelled" })}
        cancellation="confirmed"
      />,
    );
    expect(screen.getByText("Cancellation is confirmed by the server.")).toBeInTheDocument();

    view.rerender(
      <InvestigationPanel
        {...props({ ...running, status: "completed" })}
        cancellation="too_late"
      />,
    );
    expect(screen.getByText(/reached completed before cancellation took effect/i)).toBeInTheDocument();
  });

  it("treats a missing process-local job as unknown while preserving durable lifecycle state", () => {
    render(
      <InvestigationPanel
        {...props(pendingPlan({ status: "running", proposal: null }))}
        job={{ state: "unknown", reason: "not_found", cancel: "failed" }}
      />,
    );

    expect(screen.getByText(/outcome is unknown.*not restarted/i)).toBeInTheDocument();
    expect(screen.getByText(/durable server record; no work was restarted/i)).toBeInTheDocument();
  });

  it("removes statement SQL while a submitted wave awaits authoritative transition", () => {
    const waiting = pendingPlan({ status: "awaiting_sql_approval", proposal: null });
    render(
      <InvestigationPanel
        {...props(waiting)}
        command="sql_submitted"
        sqlWave={{
          wave_id: "1".repeat(32),
          wave_digest: "a".repeat(64),
          sequence: 1,
          deadline: "2026-10-30T14:00:00Z",
          items: [{
            task_id: "trend",
            attempt: 1,
            sql_fingerprint: "b".repeat(64),
            policy_status: "allowed_read_only",
            sql: "SELECT secret_statement FROM uploaded_data",
          }],
        }}
      />,
    );

    expect(screen.getByText(/statements are no longer kept in the browser/i)).toBeInTheDocument();
    expect(screen.queryByText(/SELECT secret_statement/)).not.toBeInTheDocument();
  });
});
