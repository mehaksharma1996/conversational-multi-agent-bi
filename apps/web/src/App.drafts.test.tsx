import { render, screen, waitFor } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  acceptConsent,
  askQuestion,
  confirmSchema,
  createConversation,
  createDataset,
  createWorkspace,
  decideSqlApproval,
  deleteWorkspace,
  listMessages,
  listWorkbookSheets,
  listWorkspaces,
  uploadDocuments,
  uploadTabular,
} from "./api/client";
import {
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
    deleteWorkspace: vi.fn(),
    uploadTabular: vi.fn(),
    listWorkbookSheets: vi.fn(),
    listWorkspaces: vi.fn(),
    listMessages: vi.fn(),
    createDataset: vi.fn(),
    confirmSchema: vi.fn(),
    uploadDocuments: vi.fn(),
    acceptConsent: vi.fn(),
    createConversation: vi.fn(),
    askQuestion: vi.fn(),
    decideSqlApproval: vi.fn(),
  };
});

const csvFile = () => new File(["amount,merchant\n25,North"], "transactions.csv", { type: "text/csv" });
const pdfFile = () => new File(["%PDF policy"], "policy.pdf", { type: "application/pdf" });

async function goTo(user: UserEvent, section: RegExp) {
  await user.click(screen.getByRole("link", { name: section }));
}

async function startAsk(user: UserEvent) {
  await goTo(user, /Documents/);
  await user.upload(screen.getByLabelText("PDF documents"), pdfFile());
  await user.click(screen.getByRole("button", { name: "Upload and index PDFs" }));
  await screen.findByText("Indexed 1 document(s)", { exact: false });
  await goTo(user, /Ask/);
  await user.click(screen.getByRole("button", { name: "I understand, enable Gemini" }));
}

describe("unsaved workflow drafts across section navigation", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    window.history.replaceState(null, "", "/");
    vi.mocked(listWorkspaces).mockResolvedValue([]);
    vi.mocked(listMessages).mockResolvedValue([]);
    vi.mocked(createWorkspace).mockResolvedValue(workspaceFixture);
    vi.mocked(deleteWorkspace).mockResolvedValue();
    vi.mocked(uploadTabular).mockResolvedValue(uploadFixture);
    vi.mocked(listWorkbookSheets).mockResolvedValue([]);
    vi.mocked(createDataset).mockResolvedValue(reviewDatasetFixture);
    vi.mocked(confirmSchema).mockResolvedValue(readyDatasetFixture);
    vi.mocked(uploadDocuments).mockResolvedValue(documentCollectionFixture);
    vi.mocked(acceptConsent).mockResolvedValue();
    vi.mocked(createConversation).mockResolvedValue(conversationFixture);
    vi.mocked(askQuestion).mockResolvedValue(messageFixture);
    vi.mocked(decideSqlApproval).mockResolvedValue(messageFixture);
  });

  afterEach(() => {
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it("keeps a selected dataset file and uploads that file after returning", async () => {
    const user = userEvent.setup();
    const file = csvFile();
    render(<App />);
    await screen.findByText("Local workspace ready");

    await goTo(user, /Data/);
    await user.upload(screen.getByLabelText("CSV or Excel file"), file);
    await goTo(user, /Overview/);
    await goTo(user, /Data/);

    expect(screen.getByText("Selected file:", { exact: false })).toHaveTextContent("transactions.csv");
    await user.click(screen.getByRole("button", { name: "Upload and profile" }));
    expect(uploadTabular).toHaveBeenCalledWith("workspace-1", file);
  });

  it("lets the kept dataset selection be cleared", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Local workspace ready");

    await goTo(user, /Data/);
    await user.upload(screen.getByLabelText("CSV or Excel file"), csvFile());
    await user.click(screen.getByRole("button", { name: "Clear selection" }));

    expect(screen.queryByText("Selected file:", { exact: false })).toBeNull();
    expect(screen.getByRole("button", { name: "Upload and profile" })).toBeDisabled();
  });

  it("keeps selected PDFs and indexes them after returning", async () => {
    const user = userEvent.setup();
    const file = pdfFile();
    render(<App />);
    await screen.findByText("Local workspace ready");

    await goTo(user, /Documents/);
    await user.upload(screen.getByLabelText("PDF documents"), file);
    await goTo(user, /Ask/);
    await goTo(user, /Documents/);

    expect(screen.getByText("Selected file:", { exact: false })).toHaveTextContent("policy.pdf");
    await user.click(screen.getByRole("button", { name: "Upload and index PDFs" }));
    expect(uploadDocuments).toHaveBeenCalledWith("workspace-1", [file]);
  });

  it("keeps schema mapping edits for the same dataset and drops them for a replacement", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Local workspace ready");

    await goTo(user, /Data/);
    await user.upload(screen.getByLabelText("CSV or Excel file"), csvFile());
    await user.click(screen.getByRole("button", { name: "Upload and profile" }));
    await screen.findByRole("heading", { name: "Confirm the canonical schema" });
    expect(screen.getByLabelText("merchant")).toHaveValue("merchant");
    await user.selectOptions(screen.getByLabelText("merchant"), "");

    await goTo(user, /Analysis/);
    await goTo(user, /Data/);
    expect(screen.getByLabelText("merchant")).toHaveValue("");

    // A different dataset starts from its own suggestions, not the earlier edits.
    vi.mocked(createDataset).mockResolvedValueOnce({ ...reviewDatasetFixture, id: "dataset-2" });
    await user.upload(screen.getByLabelText("CSV or Excel file"), csvFile());
    await user.click(screen.getByRole("button", { name: "Upload and profile" }));
    // Replacing a dataset now asks first (UI-2).
    await user.click(await screen.findByRole("button", { name: "Upload and replace" }));
    await waitFor(() => expect(createDataset).toHaveBeenCalledTimes(2));
    await screen.findByRole("heading", { name: "Confirm the canonical schema" });
    expect(screen.getByLabelText("merchant")).toHaveValue("merchant");
  });

  it("submits kept schema edits after returning to the section", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Local workspace ready");

    await goTo(user, /Data/);
    await user.upload(screen.getByLabelText("CSV or Excel file"), csvFile());
    await user.click(screen.getByRole("button", { name: "Upload and profile" }));
    await screen.findByRole("heading", { name: "Confirm the canonical schema" });
    await user.selectOptions(screen.getByLabelText("merchant"), "");
    await goTo(user, /Overview/);
    await goTo(user, /Data/);
    await user.click(screen.getByRole("button", { name: "Confirm schema" }));

    expect(confirmSchema).toHaveBeenCalledWith(
      "dataset-1",
      expect.objectContaining({ amount: "amount", merchant: null }),
    );
  });

  it("keeps a draft question and the approval option", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Local workspace ready");
    await startAsk(user);

    await user.type(screen.getByLabelText("Business question"), "Which stores breach policy?");
    await user.click(screen.getByRole("checkbox", { name: "Require approval before hybrid SQL runs" }));
    await goTo(user, /Data/);
    await goTo(user, /Ask/);

    expect(screen.getByLabelText("Business question")).toHaveValue("Which stores breach policy?");
    expect(screen.getByRole("checkbox", { name: "Require approval before hybrid SQL runs" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Ask workbench" }));
    expect(askQuestion).toHaveBeenCalledWith("conversation-1", "Which stores breach policy?", true);
  });

  it("keeps an in-progress SQL approval edit until the decision is made", async () => {
    const user = userEvent.setup();
    vi.mocked(askQuestion).mockResolvedValueOnce({
      ...messageFixture,
      status: "pending_approval",
      answer: "",
      rows: null,
    });
    render(<App />);
    await screen.findByText("Local workspace ready");
    await startAsk(user);
    await user.click(screen.getByLabelText("Business question"));
    await user.paste(messageFixture.question);
    await user.click(screen.getByRole("button", { name: "Ask workbench" }));
    await screen.findByRole("button", { name: "Approve SQL" });

    // A pending approval is not an answer.
    expect(screen.getByText("1 question · 1 awaiting approval")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit SQL" }));
    const edited = "SELECT merchant FROM uploaded_data LIMIT 5";
    await user.clear(screen.getByLabelText("Edit SQL"));
    await user.paste(edited);
    await goTo(user, /Overview/);
    await goTo(user, /Ask/);

    expect(screen.getByLabelText("Edit SQL")).toHaveValue(edited);
    await user.click(screen.getByRole("button", { name: "Run edited SQL" }));
    expect(decideSqlApproval).toHaveBeenCalledWith("message-1", "approve", edited);
    expect(await screen.findByText("1 question")).toBeInTheDocument();
  });

  it("clears every draft when the workspace is reset", async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Local workspace ready");

    await goTo(user, /Data/);
    await user.upload(screen.getByLabelText("CSV or Excel file"), csvFile());
    await goTo(user, /Documents/);
    await user.upload(screen.getByLabelText("PDF documents"), pdfFile());
    await goTo(user, /Ask/);
    await user.click(screen.getByRole("button", { name: "I understand, enable Gemini" }));

    await user.click(screen.getByRole("button", { name: "Reset workspace" }));
    await user.click(await screen.findByRole("button", { name: "Delete workspace" }));
    expect(deleteWorkspace).toHaveBeenCalledWith("workspace-1");
    await screen.findByText("Local workspace ready");

    await goTo(user, /Data/);
    expect(screen.queryByText("Selected file:", { exact: false })).toBeNull();
    await goTo(user, /Documents/);
    expect(screen.queryByText("Selected file:", { exact: false })).toBeNull();
  });
});
