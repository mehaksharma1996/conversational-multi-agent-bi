import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiClientError,
  getInvestigationEvidence,
  type InvestigationEvidence,
} from "../api/client";

export type ReportPhase =
  | "loading"
  | "loaded"
  /** The server has no verified report for this investigation. */
  | "none"
  /** The stored content failed its digest or contract check, so nothing is shown. */
  | "unverifiable"
  | "forbidden"
  | "error";

export interface ReportState {
  phase: ReportPhase;
  data: InvestigationEvidence | null;
}

const ANNOUNCEMENTS: Partial<Record<ReportPhase, string>> = {
  loaded: "The verified report is ready.",
  unverifiable: "The stored report could not be verified, so it is not shown.",
  forbidden: "You do not have permission to read this report.",
  error: "The verified report could not be loaded. Nothing is shown from it.",
};

function phaseOf(caught: unknown): ReportPhase {
  if (!(caught instanceof ApiClientError)) return "error";
  if (caught.status === 404) return "none";
  if (caught.status === 409) return "unverifiable";
  if (caught.status === 401 || caught.status === 403) return "forbidden";
  return "error";
}

/**
 * Reads one investigation's report and evidence once, and again only on an explicit retry. A finished
 * report is immutable, so there is no polling. The data, which can include SQL text, stays in this
 * hook's state: never in storage, logs, or announcements. Render it under a `key` of the investigation
 * ID, so another investigation starts empty.
 */
export function useInvestigationReport(investigationId: string, announce: (text: string) => void) {
  const [state, setState] = useState<ReportState>({ phase: "loading", data: null });
  const [attempt, setAttempt] = useState(0);
  const announceRef = useRef(announce);
  useEffect(() => {
    announceRef.current = announce;
  });

  useEffect(() => {
    let active = true;
    const settle = (phase: ReportPhase, data: InvestigationEvidence | null) => {
      if (!active) return;
      setState({ phase, data });
      const text = ANNOUNCEMENTS[phase];
      if (text) announceRef.current(text);
    };
    getInvestigationEvidence(investigationId).then(
      (data) => {
        // A response about another investigation is not this report.
        if (data.investigation_id === investigationId) settle("loaded", data);
        else settle("error", null);
      },
      (caught: unknown) => settle(phaseOf(caught), null),
    );
    return () => {
      active = false;
    };
  }, [investigationId, attempt]);

  const retry = useCallback(() => {
    setState({ phase: "loading", data: null });
    setAttempt((value) => value + 1);
  }, []);

  return { ...state, retry };
}
