import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  confirmSchema,
  createDataset,
  createWorkspace,
  listWorkbookSheets,
  runAnalysis,
  uploadTabular,
} from "./api/client";
import {
  analysisFixture,
  readyDatasetFixture,
  reviewDatasetFixture,
  uploadFixture,
  workspaceFixture,
} from "./test/fixtures";

vi.mock("react-plotly.js", () => ({ default: () => <div data-testid="plotly-chart" /> }));
vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    createWorkspace: vi.fn(),
    uploadTabular: vi.fn(),
    listWorkbookSheets: vi.fn(),
    createDataset: vi.fn(),
    confirmSchema: vi.fn(),
    runAnalysis: vi.fn(),
  };
});

describe("tabular analysis journey", () => {
  beforeEach(() => {
    vi.mocked(createWorkspace).mockResolvedValue(workspaceFixture);
    vi.mocked(uploadTabular).mockResolvedValue(uploadFixture);
    vi.mocked(listWorkbookSheets).mockResolvedValue([]);
    vi.mocked(createDataset).mockResolvedValue(reviewDatasetFixture);
    vi.mocked(confirmSchema).mockResolvedValue(readyDatasetFixture);
    vi.mocked(runAnalysis).mockResolvedValue(analysisFixture);
  });

  it("moves from upload through schema confirmation to deterministic results", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("Local workspace ready");
    await user.upload(
      screen.getByLabelText("CSV or Excel file"),
      new File(["amount,merchant\n25,North"], "transactions.csv", { type: "text/csv" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload and profile" }));

    expect(await screen.findByRole("heading", { name: "Confirm the canonical schema" })).toBeVisible();
    expect(screen.getByLabelText("amount")).toHaveValue("amount");
    await user.click(screen.getByRole("button", { name: "Confirm schema" }));

    expect(await screen.findByRole("heading", { name: "Configure anomaly review" })).toBeVisible();
    expect(screen.getByRole("checkbox", { name: "amount" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Run deterministic analysis" }));

    expect(await screen.findByRole("heading", { name: "Analysis dashboard" })).toBeVisible();
    expect(screen.getByText("Anomaly flags are review candidates", { exact: false })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Business analysis summary" })).toBeVisible();
    expect(runAnalysis).toHaveBeenCalledWith("dataset-1", {
      anomaly_contamination: 0.05,
      anomaly_features: ["amount"],
    });
  });
});
