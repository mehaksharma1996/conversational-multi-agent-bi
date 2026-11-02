import { useCallback, useEffect, useRef, useState } from "react";

import { ApiClientError, getConsent, type Consent } from "../api/client";

export type ConsentView =
  | { phase: "loading"; consent: null; error: null }
  | { phase: "ready"; consent: Consent; error: null }
  | { phase: "error"; consent: null; error: ApiClientError };

const LOADING: ConsentView = { phase: "loading", consent: null, error: null };

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
