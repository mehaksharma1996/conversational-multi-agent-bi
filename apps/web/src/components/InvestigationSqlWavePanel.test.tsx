import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { InvestigationSqlWave } from "../api/client";
import { InvestigationSqlWavePanel } from "./InvestigationSqlWavePanel";

function wave(overrides: Partial<InvestigationSqlWave> = {}): InvestigationSqlWave {
  return {
    wave_id: "1".repeat(32),
    wave_digest: "a".repeat(64),
    sequence: 1,
    deadline: "2026-10-30T14:00:00Z",
    items: [
      {
        task_id: "trend",
        attempt: 1,
        sql_fingerprint: "b".repeat(64),
        policy_status: "allowed_read_only",
        sql: "SELECT month, churn FROM uploaded_data",
      },
      {
        task_id: "segment",
        attempt: 2,
        sql_fingerprint: "c".repeat(64),
        policy_status: "allowed_read_only",
        sql: "SELECT segment, churn FROM uploaded_data",
      },
    ],
    ...overrides,
  };
}

describe("InvestigationSqlWavePanel", () => {
  it("requires every item to be reviewed, shows a summary, and submits exact bindings once", () => {
    const onSubmit = vi.fn();
    const value = wave();
    const view = render(<InvestigationSqlWavePanel wave={value} busy={false} onSubmit={onSubmit} />);

    expect(screen.getByRole("heading", { name: "Review SQL wave 1" })).toHaveFocus();
    expect(screen.getByText(value.items[0]!.sql)).toBeInTheDocument();
    expect(screen.getByText(/0 approve; 0 reject; 0 edit; 2 undecided/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit decisions" })).toBeDisabled();
    expect(view.container).not.toHaveTextContent(value.wave_digest);
    expect(view.container.querySelector("[aria-live], [role='status'], [role='alert']")).toBeNull();

    fireEvent.click(screen.getAllByLabelText("Approve as shown")[0]!);
    fireEvent.click(screen.getAllByLabelText("Edit and revalidate")[1]!);
    expect(screen.getByText(/1 approve; 0 reject; 1 edit; 0 undecided/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit decisions" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Edited SQL for statement 2"), {
      target: { value: " SELECT segment, COUNT(*) FROM uploaded_data GROUP BY segment " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit decisions" }));

    expect(onSubmit).toHaveBeenCalledOnce();
    expect(onSubmit).toHaveBeenCalledWith({
      wave_id: value.wave_id,
      wave_digest: value.wave_digest,
      items: [
        {
          task_id: "trend",
          sql_fingerprint: value.items[0]!.sql_fingerprint,
          decision: "approve",
        },
        {
          task_id: "segment",
          sql_fingerprint: value.items[1]!.sql_fingerprint,
          decision: "edit",
          edited_sql: "SELECT segment, COUNT(*) FROM uploaded_data GROUP BY segment",
        },
      ],
    });
  });

  it("clears all choices and edits when the authoritative wave changes", () => {
    const first = wave();
    const view = render(<InvestigationSqlWavePanel wave={first} busy={false} onSubmit={vi.fn()} />);
    fireEvent.click(screen.getAllByLabelText("Edit and revalidate")[0]!);
    fireEvent.change(screen.getByLabelText("Edited SQL for statement 1"), {
      target: { value: "SELECT 1" },
    });

    view.rerender(
      <InvestigationSqlWavePanel
        wave={wave({ wave_id: "2".repeat(32), wave_digest: "d".repeat(64), sequence: 2 })}
        busy={false}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByText(/0 approve; 0 reject; 0 edit; 2 undecided/i)).toBeInTheDocument();
    expect(screen.queryByDisplayValue("SELECT 1")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Review SQL wave 2" })).toHaveFocus();
  });

  it("keeps all controls disabled while a submission is unresolved", () => {
    render(<InvestigationSqlWavePanel wave={wave()} busy onSubmit={vi.fn()} />);

    expect(screen.getAllByRole("group")[0]).toBeDisabled();
    expect(screen.getByRole("button", { name: "Submitting decisions..." })).toBeDisabled();
  });
});
