import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  acceptConsent,
  ApiClientError,
  createWorkspace,
  deleteWorkspace,
  getConsent,
  listWorkspaces,
  withdrawConsent,
  type Consent,
} from "./api/client";
import {
  consentCurrentFixture,
  consentMissingFixture,
  consentWithdrawnFixture,
  workspaceFixture,
} from "./test/fixtures";

vi.mock("./components/PlotlyChart", () => ({ default: () => <div data-testid="plotly-chart" /> }));
vi.mock("./api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/client")>();
  return {
    ...actual,
    createWorkspace: vi.fn(),
    deleteWorkspace: vi.fn(),
    listWorkspaces: vi.fn(),
    acceptConsent: vi.fn(),
    getConsent: vi.fn(),
    withdrawConsent: vi.fn(),
  };
});

const ACCEPT = "Accept this notice for Gemini";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const announcer = () => screen.getByTestId("operation-announcer");
const panel = () => screen.getByRole("region", { name: "Workspace data-sharing consent" });

async function openAsk(user: UserEvent) {
  await screen.findByText("Local workspace ready");
  await user.click(screen.getByRole("link", { name: /Ask/ }));
}

async function confirmWithdrawal(user: UserEvent) {
  await user.click(screen.getByRole("button", { name: "Withdraw consent" }));
  const dialog = await screen.findByRole("dialog");
  await user.click(within(dialog).getByRole("button", { name: "Withdraw consent" }));
}

describe("workspace consent in the application", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    window.history.replaceState(null, "", "/");
    vi.mocked(listWorkspaces).mockResolvedValue([]);
    vi.mocked(createWorkspace).mockResolvedValue(workspaceFixture);
    vi.mocked(getConsent).mockResolvedValue(consentMissingFixture);
  });

  afterEach(() => vi.resetAllMocks());

  it("sends exactly the served version and digest, and locks questions until the server answers", async () => {
    const user = userEvent.setup();
    const answer = deferred<Consent>();
    vi.mocked(acceptConsent).mockReturnValue(answer.promise);
    render(<App />);
    await openAsk(user);

    await user.click(await screen.findByRole("button", { name: ACCEPT }));

    expect(acceptConsent).toHaveBeenCalledWith(workspaceFixture.id, consentMissingFixture.notice);
    // Pending: nothing claims success, and the repeated click cannot send a second request.
    expect(screen.getByRole("button", { name: "Accepting…" })).toBeDisabled();
    expect(panel()).toHaveAttribute("aria-busy", "true");
    expect(announcer()).toHaveTextContent("Consent update started.");
    expect(within(panel()).getByText("Not accepted")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Accepting…" }));
    expect(acceptConsent).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Accept the data-sharing notice above to enable questions.")).toBeVisible();

    vi.mocked(getConsent).mockResolvedValue(consentCurrentFixture);
    answer.resolve(consentCurrentFixture);

    await waitFor(() => expect(announcer()).toHaveTextContent("Data-sharing consent accepted for Gemini."));
    expect(within(panel()).getByText("Accepted")).toBeVisible();
    expect(screen.queryByRole("button", { name: ACCEPT })).not.toBeInTheDocument();
    expect(screen.queryByText("Accept the data-sharing notice above to enable questions.")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Workspace data-sharing consent" })).toHaveFocus());
  });

  it("reports a refused acceptance, keeps the composer locked, and shows the state read back", async () => {
    const user = userEvent.setup();
    vi.mocked(acceptConsent).mockRejectedValue(
      new ApiClientError("The notice changed after it was displayed.", "consent_notice_changed"),
    );
    render(<App />);
    await openAsk(user);
    const reads = vi.mocked(getConsent).mock.calls.length;

    await user.click(await screen.findByRole("button", { name: ACCEPT }));

    expect(await screen.findByText("The notice changed after it was displayed.")).toBeVisible();
    await waitFor(() => expect(announcer()).toHaveTextContent("Consent was not confirmed as changed."));
    expect(vi.mocked(getConsent).mock.calls.length).toBeGreaterThan(reads);
    expect(within(panel()).getByText("Not accepted")).toBeVisible();
    expect(screen.getByRole("button", { name: ACCEPT })).toBeEnabled();
  });

  it("adopts the server's state when a timed-out acceptance did reach it", async () => {
    const user = userEvent.setup();
    vi.mocked(acceptConsent).mockImplementation(() => {
      vi.mocked(getConsent).mockResolvedValue(consentCurrentFixture);
      return Promise.reject(new ApiClientError("The server took too long to respond.", "timeout"));
    });
    render(<App />);
    await openAsk(user);

    await user.click(await screen.findByRole("button", { name: ACCEPT }));

    await waitFor(() => expect(within(panel()).getByText("Accepted")).toBeVisible());
    expect(announcer()).toHaveTextContent("Consent was not confirmed as changed.");
  });

  it("withdraws only after confirmation, and only then reports it", async () => {
    const user = userEvent.setup();
    vi.mocked(getConsent).mockResolvedValue(consentCurrentFixture);
    const answer = deferred<Consent>();
    vi.mocked(withdrawConsent).mockReturnValue(answer.promise);
    render(<App />);
    await openAsk(user);
    await screen.findByRole("button", { name: "Withdraw consent" });

    await user.click(screen.getByRole("button", { name: "Withdraw consent" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Requests already sent are not recalled/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(withdrawConsent).not.toHaveBeenCalled();

    await confirmWithdrawal(user);
    expect(withdrawConsent).toHaveBeenCalledWith(workspaceFixture.id);
    expect(await screen.findByRole("button", { name: "Withdrawing…" })).toBeDisabled();
    expect(announcer()).toHaveTextContent("Consent withdrawal started.");
    expect(within(panel()).getByText("Accepted")).toBeVisible();

    vi.mocked(getConsent).mockResolvedValue(consentWithdrawnFixture);
    answer.resolve(consentWithdrawnFixture);

    await waitFor(() => expect(announcer()).toHaveTextContent("Data-sharing consent withdrawn."));
    expect(within(panel()).getByText("Withdrawn")).toBeVisible();
    expect(screen.getByRole("button", { name: ACCEPT })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Withdraw consent" })).not.toBeInTheDocument();
    expect(screen.getByText("Accept the data-sharing notice above to enable questions.")).toBeVisible();
  });

  it("restores the server's status on load, and never a remembered one", async () => {
    const user = userEvent.setup();
    vi.mocked(getConsent).mockResolvedValue(consentWithdrawnFixture);
    render(<App />);
    await openAsk(user);
    await waitFor(() => expect(within(panel()).getByText("Withdrawn")).toBeVisible());
    expect(JSON.stringify({ ...window.sessionStorage, ...window.localStorage })).not.toContain("withdrawn");
  });

  it("re-reads consent on opening Ask, so a change elsewhere is what the person sees", async () => {
    const user = userEvent.setup();
    vi.mocked(getConsent).mockResolvedValue(consentCurrentFixture);
    render(<App />);
    await openAsk(user);
    await waitFor(() => expect(within(panel()).getByText("Accepted")).toBeVisible());

    vi.mocked(getConsent).mockResolvedValue(consentWithdrawnFixture);
    await user.click(screen.getByRole("link", { name: /Overview/ }));
    await user.click(screen.getByRole("link", { name: /Ask/ }));

    await waitFor(() => expect(within(panel()).getByText("Withdrawn")).toBeVisible());
  });

  it("never shows the previous workspace's consent as the new workspace's", async () => {
    const user = userEvent.setup();
    vi.mocked(getConsent).mockResolvedValue(consentCurrentFixture);
    vi.mocked(deleteWorkspace).mockResolvedValue(undefined);
    render(<App />);
    await openAsk(user);
    await waitFor(() => expect(within(panel()).getByText("Accepted")).toBeVisible());

    const second = { ...workspaceFixture, id: "workspace-2" };
    const secondConsent = deferred<Consent>();
    vi.mocked(createWorkspace).mockResolvedValue(second);
    vi.mocked(getConsent).mockImplementation((id) =>
      id === second.id ? secondConsent.promise : Promise.resolve(consentCurrentFixture),
    );
    await user.click(screen.getByRole("button", { name: "Reset workspace" }));
    await user.click(await screen.findByRole("button", { name: "Delete workspace" }));
    await waitFor(() => expect(createWorkspace).toHaveBeenCalledTimes(2));
    await user.click(await screen.findByRole("link", { name: /Ask/ }));

    await screen.findByText("Reading this workspace's consent…");
    expect(within(panel()).queryByText("Accepted")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Withdraw consent" })).not.toBeInTheDocument();

    secondConsent.resolve({ ...consentMissingFixture, workspace_id: second.id });
    await waitFor(() => expect(within(panel()).getByText("Not accepted")).toBeVisible());
  });

  it("offers no acceptance when consent cannot be read, and recovers on retry", async () => {
    const user = userEvent.setup();
    vi.mocked(getConsent).mockRejectedValue(new ApiClientError("Server unavailable.", "network_error"));
    render(<App />);
    await openAsk(user);

    await screen.findByText(/could not be read/);
    expect(screen.queryByRole("button", { name: ACCEPT })).not.toBeInTheDocument();

    vi.mocked(getConsent).mockResolvedValue(consentMissingFixture);
    await user.click(screen.getByRole("button", { name: "Read consent again" }));

    expect(await screen.findByRole("button", { name: ACCEPT })).toBeEnabled();
  });

  it("adds no live region of its own", async () => {
    const user = userEvent.setup();
    render(<App />);
    await openAsk(user);
    await screen.findByRole("button", { name: ACCEPT });

    expect(panel().querySelector("[aria-live], [role='status'], [role='alert']")).toBeNull();
    expect(document.querySelectorAll("[data-testid='operation-announcer']")).toHaveLength(1);
  });
});
