import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Analysis } from "../api/client";
import { analysisFixture } from "../test/fixtures";
import { captureFocusIntent } from "../workspace/focus";
import { AnalysisDashboard } from "./AnalysisDashboard";

vi.mock("./PlotlyChart", () => ({ default: () => <div data-testid="plotly-chart" /> }));

describe("AnalysisDashboard", () => {
  it("shows the unique flagged total and labels the capped list as a shortlist", () => {
    const analysis = {
      ...analysisFixture,
      anomaly: {
        ...analysisFixture.anomaly,
        flagged_count: 2,
        model_flagged_count: 120,
        rule_flagged_count: 5,
        total_flagged_count: 123,
        flagged_rows: [
          { amount: 900, merchant: "North", anomaly_score: 0.8 },
          { amount: 800, merchant: "South", anomaly_score: 0.7 },
        ],
      },
    } satisfies Analysis;

    render(<AnalysisDashboard analysis={analysis} downloadBusy={false} onDownloadReport={vi.fn()} />);

    const headline = screen.getByText("Anomalies flagged").closest(".metric-card");
    expect(headline).not.toBeNull();
    expect(within(headline as HTMLElement).getByText("123")).toBeVisible();
    expect(within(headline as HTMLElement).getByText("Top 2 of 123 listed for review")).toBeVisible();

    const details = screen.getByText("Unique rows flagged").closest("div");
    expect(within(details as HTMLElement).getByText("123")).toBeVisible();
    expect(
      screen.getByRole("region", {
        name: "Review shortlist: top 2 of 123 flagged rows by anomaly score",
      }),
    ).toBeVisible();
  });

  it("omits the shortlist note when nothing was flagged", () => {
    const analysis = {
      ...analysisFixture,
      anomaly: {
        ...analysisFixture.anomaly,
        flagged_count: 0,
        model_flagged_count: 0,
        rule_flagged_count: 0,
        total_flagged_count: 0,
        flagged_rows: [],
      },
    } satisfies Analysis;

    render(<AnalysisDashboard analysis={analysis} downloadBusy={false} onDownloadReport={vi.fn()} />);

    const headline = screen.getByText("Anomalies flagged").closest(".metric-card");
    expect(within(headline as HTMLElement).getByText("0")).toBeVisible();
    expect(screen.queryByText(/listed for review/)).toBeNull();
  });

  it("shows an empty state instead of an empty shortlist when nothing was flagged", () => {
    const analysis = {
      ...analysisFixture,
      anomaly: { ...analysisFixture.anomaly, flagged_count: 0, total_flagged_count: 0, flagged_rows: [] },
    } satisfies Analysis;

    render(<AnalysisDashboard analysis={analysis} downloadBusy={false} onDownloadReport={vi.fn()} />);

    expect(screen.getByText(/No rows were flagged for review/)).toBeVisible();
    expect(screen.queryByRole("region", { name: /Review shortlist/ })).toBeNull();
  });
});

type Chart = Analysis["charts"][number];

const chart = (alternative: NonNullable<Chart["alternative"]> | null): Chart => ({
  title: "Distribution of amount",
  chart_type: "histogram",
  description: "Distribution of the primary numeric measure.",
  metadata: {},
  figure: { data: [], layout: {} },
  alternative,
});

describe("AnalysisDashboard charts (UI-4, #152)", () => {
  const alternative = {
    summary: "3 values of amount in 2 equal-width bins from 10 to 30; the busiest bin, 10 to 20, holds 2.",
    columns: ["Bin start", "Bin end", "Count"],
    rows: [
      [10, 20, 2],
      [20, 30, 1],
    ],
    total_rows: 2,
    truncated: false,
  };

  it("names each chart as a figure described by its text summary, hiding the drawing", async () => {
    render(
      <AnalysisDashboard
        analysis={{ ...analysisFixture, charts: [chart(alternative)] }}
        downloadBusy={false}
        onDownloadReport={vi.fn()}
      />,
    );

    const figure = screen.getByRole("figure", { name: "Distribution of amount" });
    expect(figure).toHaveAccessibleDescription(alternative.summary);
    const drawing = await within(figure).findByTestId("plotly-chart");
    expect(drawing.closest("[aria-hidden='true']")).not.toBeNull();
  });

  it("reveals the same numbers as a table from a keyboard-operable disclosure", async () => {
    const user = userEvent.setup();
    render(
      <AnalysisDashboard
        analysis={{ ...analysisFixture, charts: [chart(alternative)] }}
        downloadBusy={false}
        onDownloadReport={vi.fn()}
      />,
    );

    const disclosure = screen.getByText("Show the data as a table");
    disclosure.focus();
    await user.keyboard("{Enter}");

    const table = screen.getByRole("table", { name: "Distribution of amount" });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      "Bin start",
      "Bin end",
      "Count",
    ]);
    expect(within(table).getAllByRole("row")).toHaveLength(3);
  });

  it("says when the table shows only part of the data", () => {
    render(
      <AnalysisDashboard
        analysis={{
          ...analysisFixture,
          charts: [chart({ ...alternative, total_rows: 120, truncated: true })],
        }}
        downloadBusy={false}
        onDownloadReport={vi.fn()}
      />,
    );

    expect(screen.getByText("Distribution of amount (first 2 of 120 rows)", { exact: false })).toBeInTheDocument();
  });

  it("states plainly when a chart has no text alternative", () => {
    render(
      <AnalysisDashboard
        analysis={{ ...analysisFixture, charts: [chart(null)] }}
        downloadBusy={false}
        onDownloadReport={vi.fn()}
      />,
    );

    expect(screen.getByText("No text alternative is available for this chart.")).toBeVisible();
    expect(screen.queryByText("Show the data as a table")).toBeNull();
  });

  it("explains when no chart applies", () => {
    render(<AnalysisDashboard analysis={analysisFixture} downloadBusy={false} onDownloadReport={vi.fn()} />);

    expect(screen.getByText("No charts apply to this dataset's confirmed schema.")).toBeVisible();
  });

  it("focuses its heading once for a finished run and reports the request handled", async () => {
    const handled = vi.fn();
    const { unmount } = render(
      <AnalysisDashboard
        analysis={analysisFixture}
        downloadBusy={false}
        onDownloadReport={vi.fn()}
        focusRequest={captureFocusIntent()}
        onFocusRequestHandled={handled}
      />,
    );

    await waitFor(() => expect(screen.getByRole("heading", { name: "Analysis dashboard" })).toHaveFocus());
    expect(handled).toHaveBeenCalledTimes(1);
    unmount();

    // A later remount (for example, navigating back) has no request and moves nothing.
    render(<AnalysisDashboard analysis={analysisFixture} downloadBusy={false} onDownloadReport={vi.fn()} />);
    await new Promise((resolve) => window.requestAnimationFrame(() => resolve(null)));
    expect(screen.getByRole("heading", { name: "Analysis dashboard" })).not.toHaveFocus();
  });
});
