import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError, getConsent, type Consent } from "../api/client";
import {
  consentCurrentFixture,
  consentMissingFixture,
  consentWithdrawnFixture,
  workspaceFixture,
} from "../test/fixtures";
import {
  consentGateReason,
  investigationGate,
  questionGate,
  useConsent,
  type ConsentView,
} from "./useConsent";

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  getConsent: vi.fn(),
}));

const LOADING: ConsentView = { phase: "loading", consent: null, error: null };
const FAILED: ConsentView = {
  phase: "error",
  consent: null,
  error: new ApiClientError("Server unavailable.", "network_error"),
};
const ready = (consent: Consent): ConsentView => ({ phase: "ready", consent, error: null });
const localOnly = { ...workspaceFixture, local_only_mode: true, consent_required: false, data_recipients: [] };
const noRecipient = { ...workspaceFixture, consent_required: false, data_recipients: [] };
// A stale flag: the workspace record says accepted, but only the server's consent read may open.
const flaggedAccepted = { ...workspaceFixture, consent_accepted: true };
const stale: Consent = { ...consentCurrentFixture, status: "stale", questions_enabled: true };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("consent gates", () => {
  it("never treats loading or an unreadable consent as authorized, whatever the workspace flags say", () => {
    for (const workspace of [workspaceFixture, flaggedAccepted]) {
      expect(questionGate(LOADING, workspace)).toBe("loading");
      expect(questionGate(FAILED, workspace)).toBe("error");
      expect(investigationGate(LOADING, workspace)).toBe("loading");
      expect(investigationGate(FAILED, workspace)).toBe("error");
    }
  });

  it("does not block questions in a workspace with no external recipient while consent is unknown", () => {
    for (const workspace of [localOnly, noRecipient]) {
      for (const view of [LOADING, FAILED]) {
        expect(questionGate(view, workspace)).toBe("open");
      }
    }
  });

  it("opens the investigation gate on unknown consent only in local-only mode", () => {
    // The flags describe the question chain. With only an investigation model configured they read
    // as "no recipient" while the investigation's recipient is still unconfirmed.
    expect(investigationGate(LOADING, noRecipient)).toBe("loading");
    expect(investigationGate(FAILED, noRecipient)).toBe("error");
    expect(investigationGate(LOADING, localOnly)).toBe("open");
    expect(investigationGate(FAILED, localOnly)).toBe("open");
    expect(investigationGate(ready(consentCurrentFixture), noRecipient)).toBe("open");
    expect(investigationGate(ready(consentMissingFixture), noRecipient)).toBe("needs_acceptance");
  });

  it("follows the server once it answers", () => {
    expect(questionGate(ready(consentCurrentFixture), workspaceFixture)).toBe("open");
    expect(questionGate(ready(consentMissingFixture), flaggedAccepted)).toBe("needs_acceptance");
    expect(questionGate(ready(consentWithdrawnFixture), flaggedAccepted)).toBe("needs_acceptance");
  });

  it("keeps questions open under older consent but gates the investigation that needs more", () => {
    expect(questionGate(ready(stale), workspaceFixture)).toBe("open");
    expect(investigationGate(ready(stale), workspaceFixture)).toBe("needs_acceptance");
    expect(investigationGate(ready(consentCurrentFixture), workspaceFixture)).toBe("open");
  });

  it("points a caller who cannot accept to an administrator", () => {
    const viewer = ready({ ...consentMissingFixture, can_manage: false });
    expect(questionGate(viewer, workspaceFixture)).toBe("needs_administrator");
    expect(consentGateReason("needs_administrator")).toContain("workspace administrator");
    expect(consentGateReason("open")).toBeNull();
  });

  it("says why, without implying that a provider is active", () => {
    expect(consentGateReason("loading")).toContain("Checking");
    expect(consentGateReason("error")).toContain("could not be verified");
    for (const gate of ["loading", "error", "needs_acceptance", "needs_administrator"] as const) {
      expect(consentGateReason(gate)).not.toMatch(/OpenAI|active/i);
    }
  });
});

describe("useConsent", () => {
  beforeEach(() => {
    vi.mocked(getConsent).mockReset();
  });

  it("drops a read for the previous workspace that answers after the switch", async () => {
    const first = deferred<Consent>();
    const second = deferred<Consent>();
    vi.mocked(getConsent).mockImplementation((id) => (id === "one" ? first.promise : second.promise));
    const { result, rerender } = renderHook(({ id }) => useConsent(id), { initialProps: { id: "one" } });
    expect(result.current.view.phase).toBe("loading");

    rerender({ id: "two" });
    await act(async () => {
      first.resolve({ ...consentCurrentFixture, workspace_id: "one" });
      await first.promise;
    });
    expect(result.current.view.phase).toBe("loading");

    await act(async () => {
      second.resolve({ ...consentMissingFixture, workspace_id: "two" });
      await second.promise;
    });
    expect(result.current.view.consent?.status).toBe("missing");
    expect(result.current.view.consent?.workspace_id).toBe("two");
  });

  it("shows nothing from the previous workspace, even for the render before a new read", async () => {
    vi.mocked(getConsent).mockResolvedValueOnce(consentCurrentFixture);
    const { result, rerender } = renderHook(({ id }) => useConsent(id), {
      initialProps: { id: workspaceFixture.id },
    });
    await waitFor(() => expect(result.current.view.phase).toBe("ready"));

    vi.mocked(getConsent).mockReturnValue(deferred<Consent>().promise);
    rerender({ id: "two" });
    expect(result.current.view).toEqual(LOADING);
  });

  it("keeps a late mutation answer for another workspace out of the current view", async () => {
    vi.mocked(getConsent).mockResolvedValueOnce({ ...consentMissingFixture, workspace_id: "two" });
    const { result } = renderHook(() => useConsent("two"));
    await waitFor(() => expect(result.current.view.phase).toBe("ready"));

    act(() => result.current.adopt({ ...consentCurrentFixture, workspace_id: "one" }));
    expect(result.current.view.phase).toBe("loading");
  });

  it("reports an unreadable consent as an error rather than a status", async () => {
    vi.mocked(getConsent).mockRejectedValue(new ApiClientError("Server unavailable.", "network_error"));
    const { result } = renderHook(() => useConsent(workspaceFixture.id));
    await waitFor(() => expect(result.current.view.phase).toBe("error"));
    expect(result.current.view.consent).toBeNull();
  });
});
