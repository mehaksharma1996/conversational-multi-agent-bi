import { useCallback, useEffect, useRef, useState } from "react";

import { ApiClientError, cancelJob, getJob, type Job, type JobStatus } from "../api/client";
import type { OperationPhase } from "./model";

/**
 * Follows one tenant-owned background job (UI-2 S6; the base for UI-6b investigation progress).
 *
 * Jobs are process-local on the server (ADR 0020): an API restart forgets them, and their ID then
 * answers 404 exactly like a foreign one. A 404 is therefore reported as an *unknown* outcome, never
 * as success or as a confirmed failure, and nothing is re-created. The job ID grants nothing: every
 * request is authorized by the server, and a response for another job or workspace is rejected.
 *
 * Polling is one request at a time with exponential backoff (1 s doubling to 10 s, ±20% jitter) and
 * always ends: at a terminal status, a 404, 401/403, after consecutive transient failures, or at a
 * poll limit. Changing the job or workspace, or unmounting (which sign-out does), stops it and
 * discards any response still in flight.
 */

const JOB_ID = /^[A-Za-z0-9_-]{1,128}$/;
const TERMINAL = new Set<JobStatus>(["succeeded", "failed", "cancelled", "expired"]);
const ACTIVE = new Set<JobStatus>(["queued", "running"]);

export type JobCancel = "none" | "requested" | "acknowledged" | "completed" | "too_late" | "failed";

export type JobView =
  | { state: "idle" }
  | { state: "active"; status: "queued" | "running"; job: Job | null; cancel: JobCancel; retrying: boolean }
  | { state: "finished"; status: "succeeded" | "failed" | "cancelled" | "expired"; job: Job; cancel: JobCancel }
  | { state: "unknown"; reason: "not_found" | "unreachable" | "poll_limit"; cancel: JobCancel }
  | { state: "denied"; reason: "signed_out" | "forbidden" }
  | { state: "invalid" };

export interface JobPollingOptions {
  initialDelayMs?: number;
  maxDelayMs?: number;
  /** Stop after this many status reads without a terminal answer (about 30 minutes at the cap). */
  maxPolls?: number;
  /** Stop after this many consecutive network, timeout, or 5xx failures. */
  maxTransientFailures?: number;
  random?: () => number;
}

const DEFAULTS: Required<JobPollingOptions> = {
  initialDelayMs: 1_000,
  maxDelayMs: 10_000,
  maxPolls: 200,
  maxTransientFailures: 5,
  random: Math.random,
};

/** The jittered delay for the given base, within ±20%. */
export function jitter(base: number, random: () => number): number {
  return Math.round(base * (0.8 + 0.4 * random()));
}

function settledCancel(cancel: JobCancel, status: JobStatus): JobCancel {
  if (cancel !== "requested" && cancel !== "acknowledged") return cancel;
  return status === "cancelled" ? "completed" : "too_late";
}

function initialView(jobId: string | null, workspaceId: string | null): JobView {
  if (jobId === null || workspaceId === null) return { state: "idle" };
  if (!JOB_ID.test(jobId)) return { state: "invalid" };
  return { state: "active", status: "queued", job: null, cancel: "none", retrying: false };
}

function denial(error: unknown): JobView | null {
  if (!(error instanceof ApiClientError)) return null;
  if (error.status === 404) return null;
  if (error.status === 401) return { state: "denied", reason: "signed_out" };
  if (error.status === 403) return { state: "denied", reason: "forbidden" };
  return null;
}

export function useJob(jobId: string | null, workspaceId: string | null, options: JobPollingOptions = {}) {
  const settings = { ...DEFAULTS, ...options };
  const settingsRef = useRef(settings);
  useEffect(() => {
    settingsRef.current = settings;
  });
  const [restart, setRestart] = useState(0);
  // The view is stored with the job it describes; for any other job the derived initial view shows,
  // so a previous job's state can never leak into a new one.
  const key = `${jobId ?? ""}|${workspaceId ?? ""}|${restart}`;
  const [stored, setStored] = useState<{ key: string; view: JobView }>({ key: "", view: { state: "idle" } });
  const view = stored.key === key ? stored.view : initialView(jobId, workspaceId);
  const keyRef = useRef(key);
  const generation = useRef(0);
  const cancelState = useRef<JobCancel>("none");
  const target = useRef<{ jobId: string; workspaceId: string } | null>(null);

  const setView = useCallback((next: JobView | ((previous: JobView) => JobView)) => {
    const owner = keyRef.current;
    setStored((previous) => {
      const [jobPart = "", workspacePart = ""] = owner.split("|");
      const base = previous.key === owner ? previous.view : initialView(jobPart || null, workspacePart || null);
      return { key: owner, view: typeof next === "function" ? next(base) : next };
    });
  }, []);

  useEffect(() => {
    const current = ++generation.current;
    const stale = () => current !== generation.current;
    let timer: ReturnType<typeof setTimeout> | null = null;
    keyRef.current = `${jobId ?? ""}|${workspaceId ?? ""}|${restart}`;
    cancelState.current = "none";
    target.current = null;
    if (jobId === null || workspaceId === null || !JOB_ID.test(jobId)) return;
    target.current = { jobId, workspaceId };
    const { initialDelayMs, maxDelayMs, maxPolls, maxTransientFailures, random } = settingsRef.current;
    let polls = 0;
    let failures = 0;
    let delay = initialDelayMs;

    const schedule = () => {
      timer = setTimeout(() => void poll(), jitter(delay, random));
      delay = Math.min(delay * 2, maxDelayMs);
    };

    const poll = async () => {
      if (stale()) return;
      polls += 1;
      try {
        const job = await getJob(jobId);
        if (stale()) return;
        if (job.id !== jobId || job.workspace_id !== workspaceId || !(TERMINAL.has(job.status) || ACTIVE.has(job.status))) {
          setView({ state: "invalid" });
          return;
        }
        failures = 0;
        if (TERMINAL.has(job.status)) {
          cancelState.current = settledCancel(cancelState.current, job.status);
          setView({
            state: "finished",
            status: job.status as "succeeded" | "failed" | "cancelled" | "expired",
            job,
            cancel: cancelState.current,
          });
          return;
        }
        if (job.cancel_requested && (cancelState.current === "none" || cancelState.current === "requested")) {
          cancelState.current = "acknowledged";
        }
        setView({
          state: "active",
          status: job.status as "queued" | "running",
          job,
          cancel: cancelState.current,
          retrying: false,
        });
      } catch (caught) {
        if (stale()) return;
        const denied = denial(caught);
        if (denied) {
          setView(denied);
          return;
        }
        if (caught instanceof ApiClientError && caught.status === 404) {
          setView({ state: "unknown", reason: "not_found", cancel: cancelState.current });
          return;
        }
        if (!(caught instanceof ApiClientError) || !caught.retryable) {
          setView({ state: "invalid" });
          return;
        }
        failures += 1;
        if (failures >= maxTransientFailures) {
          setView({ state: "unknown", reason: "unreachable", cancel: cancelState.current });
          return;
        }
        setView((previous) =>
          previous.state === "active" ? { ...previous, retrying: true } : previous,
        );
      }
      if (polls >= maxPolls) {
        setView({ state: "unknown", reason: "poll_limit", cancel: cancelState.current });
        return;
      }
      schedule();
    };

    void poll();
    return () => {
      generation.current += 1;
      if (timer !== null) clearTimeout(timer);
    };
  }, [jobId, workspaceId, restart, setView]);

  /** Requests cancellation once; the job keeps its status until the server reports a terminal one. */
  const cancel = useCallback(async () => {
    const current = generation.current;
    const ids = target.current;
    if (!ids || (cancelState.current !== "none" && cancelState.current !== "failed")) return;
    cancelState.current = "requested";
    setView((previous) => (previous.state === "active" ? { ...previous, cancel: "requested" } : previous));
    try {
      const job = await cancelJob(ids.jobId);
      if (current !== generation.current) return;
      if (job.id !== ids.jobId || job.workspace_id !== ids.workspaceId) {
        setView({ state: "invalid" });
        return;
      }
      if (TERMINAL.has(job.status)) {
        cancelState.current = settledCancel(cancelState.current, job.status);
        setView({
          state: "finished",
          status: job.status as "succeeded" | "failed" | "cancelled" | "expired",
          job,
          cancel: cancelState.current,
        });
        generation.current += 1; // the answer is final; stop the poll loop
        return;
      }
      if (job.cancel_requested) cancelState.current = "acknowledged";
      setView((previous) => (previous.state === "active" ? { ...previous, cancel: cancelState.current } : previous));
    } catch (caught) {
      if (current !== generation.current) return;
      if (caught instanceof ApiClientError && caught.status === 404) {
        cancelState.current = "failed";
        generation.current += 1;
        setView({ state: "unknown", reason: "not_found", cancel: "failed" });
        return;
      }
      const denied = denial(caught);
      if (denied) {
        generation.current += 1;
        setView(denied);
        return;
      }
      cancelState.current = "failed";
      setView((previous) => (previous.state === "active" ? { ...previous, cancel: "failed" } : previous));
    }
  }, [setView]);

  /** Starts a fresh read after polling stopped without an answer (unreachable or poll limit). */
  const refresh = useCallback(() => setRestart((value) => value + 1), []);

  return { view, cancel, refresh };
}

/** The operation phase a job view corresponds to; unknown outcomes are `interrupted`, never success. */
export function jobPhase(view: JobView): OperationPhase | null {
  switch (view.state) {
    case "active":
      return view.cancel === "requested" || view.cancel === "acknowledged" ? "cancelling" : view.status;
    case "finished":
      return view.status === "expired" ? "interrupted" : view.status;
    case "unknown":
      return "interrupted";
    default:
      return null;
  }
}

/** Plain status text without job identifiers or server error details. */
export function describeJob(view: JobView, label: string): string {
  switch (view.state) {
    case "idle":
      return "";
    case "active": {
      const progress =
        view.job && view.job.progress_total
          ? ` (${view.job.progress_completed} of ${view.job.progress_total})`
          : "";
      if (view.cancel === "requested") return `Cancelling ${label}: request sent; it is still running until the server confirms it stopped.`;
      if (view.cancel === "acknowledged") return `Cancelling ${label}: the server accepted the request; work already in progress may still finish.`;
      const retry = view.retrying ? " Reconnecting to the server…" : "";
      const failed = view.cancel === "failed" ? " The cancellation request failed; it is still running." : "";
      return `${label} is ${view.status === "queued" ? "queued" : "running"}${progress}.${failed}${retry}`;
    }
    case "finished":
      if (view.cancel === "completed") return `${label} was cancelled.`;
      if (view.cancel === "too_late") return `${label} ${view.status === "succeeded" ? "finished" : "ended"} before the cancellation took effect.`;
      if (view.status === "succeeded") return `${label} finished.`;
      if (view.status === "failed") return `${label} failed.`;
      if (view.status === "cancelled") return `${label} was cancelled.`;
      return `${label} expired before it finished; its outcome is unknown.`;
    case "unknown":
      if (view.reason === "not_found") return `The server no longer has a record of ${label} (it may have restarted, or the record expired), so its outcome is unknown. It was not restarted.`;
      if (view.reason === "unreachable") return `The server could not be reached, so the status of ${label} is unknown. Check again when the connection is back.`;
      return `${label} is taking longer than expected; status checks have stopped, so its outcome is not yet known. Check again.`;
    case "denied":
      return view.reason === "signed_out" ? "Sign in again to see this status." : "You do not have access to this status.";
    case "invalid":
      return `The status of ${label} could not be read.`;
  }
}
