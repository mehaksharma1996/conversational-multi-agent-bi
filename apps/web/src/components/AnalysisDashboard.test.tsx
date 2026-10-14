import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Analysis } from "../api/client";
import { analysisFixture } from "../test/fixtures";
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
});
