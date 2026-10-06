import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  acceptConsent,
  askQuestion,
  confirmSchema,
  createConversation,
  createDataset,
  createWorkspace,
  decideSqlApproval,
  downloadWorkspaceExport,
  listWorkbookSheets,
  runAnalysis,
  uploadDocuments,
  uploadTabular,
} from "./api/client";
import {
  analysisFixture,
  conversationFixture,
  documentCollectionFixture,
  messageFixture,
  readyDatasetFixture,
  reviewDatasetFixture,
  uploadFixture,
  workspaceFixture,
} from "./test/fixtures";

vi.mock("./components/PlotlyChart", () => ({ default: () => <div data-testid="plotly-chart" /> }));
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
    uploadDocuments: vi.fn(),
    acceptConsent: vi.fn(),
    createConversation: vi.fn(),
    askQuestion: vi.fn(),
    decideSqlApproval: vi.fn(),
    downloadWorkspaceExport: vi.fn(),
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
    vi.mocked(uploadDocuments).mockResolvedValue(documentCollectionFixture);
    vi.mocked(acceptConsent).mockResolvedValue();
    vi.mocked(createConversation).mockResolvedValue(conversationFixture);
    vi.mocked(askQuestion).mockResolvedValue(messageFixture);
    vi.mocked(decideSqlApproval).mockResolvedValue(messageFixture);
  });

  it("tells the user how long data is kept and lets them export it", async () => {
    const user = userEvent.setup();
    const blob = new Blob(["zip bytes"], { type: "application/zip" });
    vi.mocked(downloadWorkspaceExport).mockResolvedValue(blob);
    const createUrl = vi.fn(() => "blob:workspace-export");
    const revokeUrl = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: revokeUrl }));
    render(<App />);

    await screen.findByText("Local workspace ready");
    expect(screen.getByTestId("retention-notice")).toHaveTextContent(
      "deleted 24 hours after its last use",
    );
    await user.click(screen.getByRole("button", { name: "Export my data" }));

    expect(downloadWorkspaceExport).toHaveBeenCalledWith("workspace-1");
    expect(createUrl).toHaveBeenCalledWith(blob);
    expect(revokeUrl).toHaveBeenCalledWith("blob:workspace-export");
    vi.unstubAllGlobals();
  });

  it("shows a safe error when the export fails", async () => {
    const user = userEvent.setup();
    vi.mocked(downloadWorkspaceExport).mockRejectedValue(new Error("boom"));
    render(<App />);

    await screen.findByText("Local workspace ready");
    await user.click(screen.getByRole("button", { name: "Export my data" }));

    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.getByRole("button", { name: "Export my data" })).toBeEnabled();
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
    expect(screen.getByText("Anomaly flags and classifier scores", { exact: false })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Supervised classification" })).toBeVisible();
    expect(screen.getByText("Classifier candidates for human review")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Business analysis summary" })).toBeVisible();
    expect(runAnalysis).toHaveBeenCalledWith("dataset-1", {
      anomaly_contamination: 0.05,
      anomaly_features: ["amount"],
    });
  });

  it("indexes documents, records model consent, and renders a cited answer", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("Local workspace ready");
    await user.upload(
      screen.getByLabelText("PDF documents"),
      new File(["%PDF policy"], "policy.pdf", { type: "application/pdf" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload and index PDFs" }));

    expect(await screen.findByText("Indexed 1 document(s)", { exact: false })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "I understand, enable Gemini" }));

    await user.type(
      screen.getByLabelText("Business question"),
      "Which transaction matches the escalation policy?",
    );
    await user.click(screen.getByRole("button", { name: "Ask workbench" }));

    expect(await screen.findByText(messageFixture.answer)).toBeVisible();
    expect(screen.getByText("hybrid route")).toBeVisible();
    expect(screen.getByText("Request ID: req-fixture-0001")).toBeVisible();
    expect(screen.getByText(/Decision support only/)).toBeVisible();
    await user.click(screen.getByText("Retrieved document sources"));
    expect(screen.getByText("policy.pdf, page 2")).toBeVisible();
    expect(createConversation).toHaveBeenCalledWith("workspace-1", null, "documents-1");
    expect(askQuestion).toHaveBeenCalledWith("conversation-1", messageFixture.question, false);
  });

  it("pauses hybrid SQL for an explicit keyboard-accessible approval", async () => {
    const user = userEvent.setup();
    const pendingMessage = {
      ...messageFixture,
      status: "pending_approval" as const,
      answer: "",
      rows: null,
    };
    vi.mocked(askQuestion).mockResolvedValueOnce(pendingMessage);
    render(<App />);

    await screen.findByText("Local workspace ready");
    await user.upload(
      screen.getByLabelText("PDF documents"),
      new File(["%PDF policy"], "policy.pdf", { type: "application/pdf" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload and index PDFs" }));
    await user.click(screen.getByRole("button", { name: "I understand, enable Gemini" }));
    await user.click(
      screen.getByRole("checkbox", { name: "Require approval before hybrid SQL runs" }),
    );
    await user.type(screen.getByLabelText("Business question"), messageFixture.question);
    await user.click(screen.getByRole("button", { name: "Ask workbench" }));

    expect(await screen.findByText("Review the proposed SQL", { exact: false })).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve SQL" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reject SQL" })).toBeVisible();
    expect(askQuestion).toHaveBeenCalledWith(
      "conversation-1",
      messageFixture.question,
      true,
    );
    await user.click(screen.getByRole("button", { name: "Approve SQL" }));

    expect(decideSqlApproval).toHaveBeenCalledWith("message-1", "approve");
    expect(await screen.findByText(messageFixture.answer)).toBeVisible();
  });
});
