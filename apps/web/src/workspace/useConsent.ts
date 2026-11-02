import { useCallback, useEffect, useRef, useState } from "react";

import { ApiClientError, getConsent, type Consent, type Workspace } from "../api/client";

export type ConsentView =
  | { phase: "loading"; consent: null; error: null }
  | { phase: "ready"; consent: Consent; error: null }
  | { phase: "error"; consent: null; error: ApiClientError };

const LOADING: ConsentView = { phase: "loading", consent: null, error: null };

/** Whether an action that may send data can be offered: only the server's answer opens it. */
export type ConsentGate = "open" | "loading" | "error" | "needs_acceptance" | "needs_administrator";

/**
 * Local-only and recipient-free workspaces have nothing to consent to, so an unknown consent must
 * not block them. These flags can only ever keep such a workspace usable, never authorize sharing.
 */
function mayShareData(workspace: Workspace): boolean {
  return !workspace.local_only_mode && (workspace.consent_required || (workspace.data_recipients?.length ?? 0) > 0);
}

function gate(view: ConsentView, workspace: Workspace, authorized: (consent: Consent) => boolean): ConsentGate {
  if (view.phase !== "ready") return mayShareData(workspace) ? view.phase : "open";
  if (authorized(view.consent)) return "open";
  return view.consent.can_manage ? "needs_acceptance" : "needs_administrator";
}

/** The gate on model-backed questions. */
export function questionGate(view: ConsentView, workspace: Workspace): ConsentGate {
  return gate(view, workspace, (consent) => consent.questions_enabled);
}

/** The gate on starting an investigation, which may also need the investigation model's recipient. */
export function investigationGate(view: ConsentView, workspace: Workspace): ConsentGate {
  return gate(view, workspace, (consent) => consent.status === "current" || consent.status === "not_required");
}

const GATE_REASON: Record<Exclude<ConsentGate, "open">, string> = {
  loading: "Checking this workspace's data-sharing consent. This is available once the server confirms it.",
  error:
    "This workspace's data-sharing consent could not be verified, so anything that may send data to an external recipient is unavailable. Read consent again above.",
  needs_acceptance: "Accept the data-sharing notice above to enable questions and investigations.",
  needs_administrator:
    "A workspace administrator must accept the data-sharing notice above to enable questions and investigations.",
};

/** Why a gated action is unavailable, or null when it is not gated. */
export function consentGateReason(consentGate: ConsentGate): string | null {
  return consentGate === "open" ? null : GATE_REASON[consentGate];
}

function asClientError(caught: unknown): ApiClientError {
  return caught instanceof ApiClientError
    ? caught
    : new ApiClientError("Could not read this workspace's consent.", "network_error");
}

/**
 * The server's consent state for one workspace. It never outlives the workspace it was read for:
 * the view is derived from the workspace ID at render time, so another workspace's consent cannot
 * be shown as current, even for the render before the new read starts. The newest request wins.
 */
export function useConsent(workspaceId: string | null) {
  const [loaded, setLoaded] = useState<{ workspaceId: string; view: ConsentView } | null>(null);
  const latest = useRef(0);

  /** Re-reads the server. With `keepOnFailure`, a failed read leaves the last confirmed state. */
  const refresh = useCallback(
    async (keepOnFailure = false): Promise<void> => {
      if (workspaceId === null) return;
      latest.current += 1;
      const request = latest.current;
      try {
        const consent = await getConsent(workspaceId);
        if (request !== latest.current) return;
        setLoaded({ workspaceId, view: { phase: "ready", consent, error: null } });
      } catch (caught) {
        if (request !== latest.current || keepOnFailure) return;
        setLoaded({
          workspaceId,
          view: { phase: "error", consent: null, error: asClientError(caught) },
        });
      }
    },
    [workspaceId],
  );

  /** Takes a state the server just answered with; any read still in flight is older, so dropped. */
  const adopt = useCallback((consent: Consent) => {
    latest.current += 1;
    setLoaded({
      workspaceId: consent.workspace_id,
      view: { phase: "ready", consent, error: null },
    });
  }, []);

  useEffect(() => {
    latest.current += 1; // a read for the previous workspace can no longer land
    // eslint-disable-next-line react-hooks/set-state-in-effect -- refresh() only sets state after awaiting the API
    void refresh();
  }, [refresh]);

  const view = workspaceId !== null && loaded?.workspaceId === workspaceId ? loaded.view : LOADING;
  return { view, refresh, adopt };
}
