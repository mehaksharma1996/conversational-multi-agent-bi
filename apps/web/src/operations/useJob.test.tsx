import { act, render, renderHook, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError, cancelJob, getJob, type Job } from "../api/client";
import { JobProgress } from "../components/JobProgress";
import { describeJob, jitter, jobPhase, useJob, type JobPollingOptions, type JobView } from "./useJob";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, getJob: vi.fn(), cancelJob: vi.fn() };
});

const JOB_ID = "0123456789abcdef0123456789abcdef";
const WORKSPACE = "ws_one";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: JOB_ID,
    workspace_id: WORKSPACE,
    operation: "investigation.plan",
    status: "running",
    progress_stage: null,
    progress_completed: 0,
    progress_total: null,
    error_category: null,
    cancel_requested: false,
    retryable: false,
    attempt: 1,
    max_attempts: 1,
    request_id: null,
    created_at: "2026-10-29T10:00:00Z",
    updated_at: "2026-10-29T10:00:00Z",
    expires_at: "2026-10-29T11:00:00Z",
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const fixed: JobPollingOptions = { random: () => 0.5 };
const network = () => new ApiClientError("Could not reach the server.", "network_error");
const status = (code: number) => new ApiClientError("refused", "x", null, code);

function follow(jobId: string | null = JOB_ID, workspaceId: string | null = WORKSPACE, options = fixed) {
  return renderHook((props: { jobId: string | null; workspaceId: string | null }) => useJob(props.jobId, props.workspaceId, options), {
    initialProps: { jobId, workspaceId },
  });
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(getJob).mockReset();
  vi.mocked(cancelJob).mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useJob polling", () => {
  it("reads at once, then follows queued → running → succeeded with backoff", async () => {
    vi.mocked(getJob)
      .mockResolvedValueOnce(job({ status: "queued" }))
      .mockResolvedValueOnce(job({ status: "running", progress_completed: 1, progress_total: 3 }))
      .mockResolvedValueOnce(job({ status: "succeeded" }));
    const { result } = follow();
    await advance(0);
    expect(getJob).toHaveBeenCalledTimes(1);
    expect(result.current.view).toMatchObject({ state: "active", status: "queued" });

    await advance(999);
    expect(getJob).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(result.current.view).toMatchObject({ state: "active", status: "running" });
    await advance(2_000);
    expect(result.current.view).toMatchObject({ state: "finished", status: "succeeded" });
    await advance(60_000);
    expect(getJob).toHaveBeenCalledTimes(3);
  });

  it("reports a terminal failure and stops", async () => {
    vi.mocked(getJob).mockResolvedValue(job({ status: "failed", error_category: "provider" }));
    const { result } = follow();
    await advance(0);
    expect(result.current.view).toMatchObject({ state: "finished", status: "failed" });
    expect(jobPhase(result.current.view)).toBe("failed");
    await advance(60_000);
    expect(getJob).toHaveBeenCalledTimes(1);
  });

  it("backs off 1, 2, 4, 8, then 10 s and stops at the poll limit with an unknown outcome", async () => {
    vi.mocked(getJob).mockResolvedValue(job());
    const { result } = follow(JOB_ID, WORKSPACE, { ...fixed, maxPolls: 7 });
    const times: number[] = [];
    let elapsed = 0;
    await advance(0);
    for (let step = 0; step < 200 && vi.mocked(getJob).mock.calls.length < 7; step += 1) {
      const before = vi.mocked(getJob).mock.calls.length;
      await advance(500);
      elapsed += 500;
      if (vi.mocked(getJob).mock.calls.length > before) times.push(elapsed);
    }
    expect(times).toEqual([1_000, 3_000, 7_000, 15_000, 25_000, 35_000]);
    expect(result.current.view).toEqual({ state: "unknown", reason: "poll_limit", cancel: "none" });
    await advance(60_000);
    expect(getJob).toHaveBeenCalledTimes(7);
  });

  it("jitter stays within ±20%", () => {
    expect(jitter(1_000, () => 0)).toBe(800);
    expect(jitter(1_000, () => 1)).toBe(1_200);
  });

  it("never runs two reads at once", async () => {
    const slow = deferred<Job>();
    vi.mocked(getJob).mockReturnValue(slow.promise);
    follow();
    await advance(120_000);
    expect(getJob).toHaveBeenCalledTimes(1);
  });

  it("stops on unmount (as sign-out does) and ignores the read still in flight", async () => {
    const slow = deferred<Job>();
    vi.mocked(getJob).mockReturnValue(slow.promise);
    const { unmount } = follow();
    await advance(0);
    unmount();
    slow.resolve(job({ status: "succeeded" }));
    await advance(60_000);
    expect(getJob).toHaveBeenCalledTimes(1);
  });

  it("switching workspace drops the old job's late answer", async () => {
    const old = deferred<Job>();
    vi.mocked(getJob).mockReturnValueOnce(old.promise).mockResolvedValueOnce(job({ workspace_id: "ws_two", status: "queued" }));
    const { result, rerender } = follow();
    await advance(0);
    rerender({ jobId: JOB_ID, workspaceId: "ws_two" });
    await advance(0);
    old.resolve(job({ status: "succeeded" }));
    await advance(0);
    expect(result.current.view).toMatchObject({ state: "active", status: "queued" });
  });

  it("a different job never shows the previous job's state", async () => {
    vi.mocked(getJob).mockResolvedValueOnce(job({ status: "failed" }));
    const { result, rerender } = follow();
    await advance(0);
    expect(result.current.view).toMatchObject({ state: "finished", status: "failed" });
    const next = deferred<Job>();
    vi.mocked(getJob).mockReturnValueOnce(next.promise);
    rerender({ jobId: "fedcba9876543210fedcba9876543210", workspaceId: WORKSPACE });
    expect(result.current.view).toMatchObject({ state: "active", status: "queued", job: null });
  });

  it("a 404 (restart or expiry) is an unknown outcome, never success, and is not re-created", async () => {
    vi.mocked(getJob).mockResolvedValueOnce(job()).mockRejectedValueOnce(status(404));
    const { result } = follow();
    await advance(1_000);
    expect(result.current.view).toEqual({ state: "unknown", reason: "not_found", cancel: "none" });
    expect(jobPhase(result.current.view)).toBe("interrupted");
    expect(describeJob(result.current.view, "the investigation")).toContain("its outcome is unknown");
    await advance(60_000);
    expect(getJob).toHaveBeenCalledTimes(2);
  });

  it.each([
    [401, "signed_out"],
    [403, "forbidden"],
  ])("stops on %i without retrying", async (code, reason) => {
    vi.mocked(getJob).mockRejectedValue(status(code));
    const { result } = follow();
    await advance(60_000);
    expect(result.current.view).toEqual({ state: "denied", reason });
    expect(getJob).toHaveBeenCalledTimes(1);
  });

  it("retries transient failures with backoff, then gives up as unreachable; Check again restarts", async () => {
    vi.mocked(getJob).mockResolvedValueOnce(job()).mockRejectedValueOnce(network());
    const { result } = follow(JOB_ID, WORKSPACE, { ...fixed, maxTransientFailures: 3 });
    await advance(1_000);
    expect(result.current.view).toMatchObject({ state: "active", retrying: true });

    vi.mocked(getJob).mockRejectedValue(network());
    await advance(60_000);
    expect(result.current.view).toEqual({ state: "unknown", reason: "unreachable", cancel: "none" });
    expect(getJob).toHaveBeenCalledTimes(4);

    vi.mocked(getJob).mockResolvedValue(job({ status: "succeeded" }));
    act(() => result.current.refresh());
    await advance(0);
    expect(result.current.view).toMatchObject({ state: "finished", status: "succeeded" });
  });

  it.each([
    ["an unknown status", job({ status: "paused" as Job["status"] })],
    ["another job", job({ id: "ffffffffffffffffffffffffffffffff" })],
    ["another workspace", job({ workspace_id: "ws_other" })],
  ])("rejects a response for %s", async (_label, response) => {
    vi.mocked(getJob).mockResolvedValue(response);
    const { result } = follow();
    await advance(60_000);
    expect(result.current.view).toEqual({ state: "invalid" });
    expect(getJob).toHaveBeenCalledTimes(1);
  });

  it("sends nothing for a missing or malformed job identifier", async () => {
    const idle = follow(null);
    expect(idle.result.current.view).toEqual({ state: "idle" });
    const bad = follow("../workspaces/ws_one");
    expect(bad.result.current.view).toEqual({ state: "invalid" });
    await advance(60_000);
    expect(getJob).not.toHaveBeenCalled();
  });
});

describe("useJob cancellation", () => {
  it("separates requested, acknowledged, and completed", async () => {
    vi.mocked(getJob)
      .mockResolvedValueOnce(job())
      .mockResolvedValueOnce(job({ cancel_requested: true }))
      .mockResolvedValueOnce(job({ status: "cancelled", cancel_requested: true }));
    const pending = deferred<Job>();
    vi.mocked(cancelJob).mockReturnValue(pending.promise);
    const { result } = follow();
    await advance(0);

    act(() => void result.current.cancel());
    expect(result.current.view).toMatchObject({ state: "active", cancel: "requested" });
    expect(jobPhase(result.current.view)).toBe("cancelling");
    act(() => void result.current.cancel());
    expect(cancelJob).toHaveBeenCalledTimes(1);

    pending.resolve(job({ cancel_requested: true }));
    await advance(0);
    expect(result.current.view).toMatchObject({ state: "active", status: "running", cancel: "acknowledged" });
    expect(describeJob(result.current.view, "the investigation")).toContain("may still finish");

    await advance(3_000);
    expect(result.current.view).toMatchObject({ state: "finished", status: "cancelled", cancel: "completed" });
  });

  it("says when the job finished before the cancellation took effect", async () => {
    vi.mocked(getJob).mockResolvedValueOnce(job()).mockResolvedValueOnce(job({ status: "succeeded" }));
    vi.mocked(cancelJob).mockResolvedValue(job({ cancel_requested: true }));
    const { result } = follow();
    await advance(0);
    await act(async () => {
      await result.current.cancel();
    });
    await advance(1_000);
    expect(result.current.view).toMatchObject({ state: "finished", status: "succeeded", cancel: "too_late" });
    expect(describeJob(result.current.view, "The investigation")).toBe(
      "The investigation finished before the cancellation took effect.",
    );
  });

  it("a failed cancellation request keeps the job running and can be retried", async () => {
    vi.mocked(getJob).mockResolvedValue(job());
    vi.mocked(cancelJob).mockRejectedValueOnce(network());
    const { result } = follow();
    await advance(0);
    await act(async () => {
      await result.current.cancel();
    });
    expect(result.current.view).toMatchObject({ state: "active", status: "running", cancel: "failed" });
    expect(describeJob(result.current.view, "the investigation")).toContain("still running");
    vi.mocked(cancelJob).mockResolvedValueOnce(job({ cancel_requested: true }));
    await act(async () => {
      await result.current.cancel();
    });
    expect(cancelJob).toHaveBeenCalledTimes(2);
    expect(result.current.view).toMatchObject({ cancel: "acknowledged" });
  });

  it("a cancellation answered with 404 is an unknown outcome", async () => {
    vi.mocked(getJob).mockResolvedValue(job());
    vi.mocked(cancelJob).mockRejectedValue(status(404));
    const { result } = follow();
    await advance(0);
    await act(async () => {
      await result.current.cancel();
    });
    expect(result.current.view).toEqual({ state: "unknown", reason: "not_found", cancel: "failed" });
    await advance(60_000);
    expect(getJob).toHaveBeenCalledTimes(1);
  });
});

describe("JobProgress", () => {
  const running: JobView = {
    state: "active",
    status: "running",
    job: job({ progress_completed: 2, progress_total: 5 }),
    cancel: "none",
    retrying: false,
  };

  it("shows determinate progress only from a reported total, and a Cancel action", async () => {
    vi.useRealTimers();
    const onCancel = vi.fn();
    render(<JobProgress label="the investigation" view={running} onCancel={onCancel} />);
    expect(screen.getByRole("progressbar", { name: "Progress of the investigation" })).toHaveAttribute("value", "2");
    expect(screen.getByText("the investigation is running (2 of 5).")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Cancel the investigation" }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(document.body.textContent).not.toContain(JOB_ID);
  });

  it("offers Check again only when polling stopped unanswered", () => {
    const { rerender } = render(
      <JobProgress label="the investigation" view={{ state: "unknown", reason: "unreachable", cancel: "none" }} onRefresh={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Check again" })).toBeVisible();
    rerender(
      <JobProgress label="the investigation" view={{ state: "unknown", reason: "not_found", cancel: "none" }} onRefresh={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });
});
