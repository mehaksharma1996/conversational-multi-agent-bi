import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiClientError,
  cancelInvestigation,
  createInvestigation,
  decideInvestigationPlan,
  decideInvestigationSqlWave,
  getInvestigation,
  getInvestigationByMessage,
  getInvestigationSqlWave,
  type Investigation,
} from "../api/client";
import type { OperationHandle } from "../operations/useOperations";
import { useInvestigation } from "./useInvestigation";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    cancelInvestigation: vi.fn(),
    createInvestigation: vi.fn(),
    decideInvestigationPlan: vi.fn(),
    decideInvestigationSqlWave: vi.fn(),
    getInvestigation: vi.fn(),
    getInvestigationByMessage: vi.fn(),
    getInvestigationSqlWave: vi.fn(),
  };
});

const WORKSPACE = "ws_one";
const CONVERSATION = "conv_one";
const MESSAGE = "msg_one";

function investigation(overrides: Partial<Investigation> = {}): Investigation {
  return {
    id: "inv_one",
    conversation_id: CONVERSATION,
    message_id: MESSAGE,
    status: "planning",
    created_at: "2026-10-30T10:00:00Z",
    updated_at: "2026-10-30T10:00:00Z",
    ...overrides,
  };
}

function pendingPlan(): Investigation {
  return investigation({
    status: "awaiting_plan_approval",
    plan: { schema_version: 1, objective: "Review churn", sub_questions: ["Why?"], tasks: [] },
    plan_digest: "a".repeat(64),
    authority_epoch: 1,
    proposal: {
      cost_estimate: {
        kind: "estimate",
        basis: "token_limits_x_highest_eligible_price",
        bound: "enforced_upper_bound",
        amount_usd: 0.1,
        limitations: [],
      },
      missing_information: [],
      approval_requirements: [{ kind: "plan", status: "required" }],
      plan_expires_at: "2026-10-31T10:00:00Z",
    },
  });
}

function waitingForSql(): Investigation {
  return investigation({
    status: "awaiting_sql_approval",
    sql_wave: {
      wave_id: "1".repeat(32),
      wave_digest: "a".repeat(64),
      sequence: 1,
      deadline: "2026-10-30T11:00:00Z",
      items: [
        {
          task_id: "trend",
          attempt: 1,
          sql_fingerprint: "b".repeat(64),
          policy_status: "allowed_read_only",
        },
      ],
    },
  });
}

function notFound() {
  return new ApiClientError("not found", "not_found", null, 404);
}

function network() {
  return new ApiClientError("network", "network_error");
}

function operation(): OperationHandle {
  return {
    id: "op_one",
    kind: "investigation",
    advance: vi.fn(),
    progress: vi.fn(),
    finish: vi.fn(),
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function follow(beginCommand = vi.fn(() => operation())) {
  return renderHook(
    (props: { workspaceId: string; conversationId: string; proposalMessageId: string }) =>
      useInvestigation({
        ...props,
        beginCommand,
        polling: { random: () => 0.5 },
        newIdempotencyKey: () => "key_one",
      }),
    {
      initialProps: {
        workspaceId: WORKSPACE,
        conversationId: CONVERSATION,
        proposalMessageId: MESSAGE,
      },
    },
  );
}

beforeEach(() => {
  vi.mocked(cancelInvestigation).mockReset();
  vi.mocked(createInvestigation).mockReset();
  vi.mocked(decideInvestigationPlan).mockReset();
  vi.mocked(decideInvestigationSqlWave).mockReset();
  vi.mocked(getInvestigation).mockReset();
  vi.mocked(getInvestigationByMessage).mockReset();
  vi.mocked(getInvestigationSqlWave).mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("investigation discovery and creation", () => {
  it("discovers with a read, then creates once with one in-memory idempotency key", async () => {
    vi.mocked(getInvestigationByMessage).mockRejectedValue(notFound());
    vi.mocked(createInvestigation).mockResolvedValue(investigation());
    const begin = vi.fn(() => operation());
    const { result } = follow(begin);
    await waitFor(() => expect(result.current.availability).toBe("available"));
    await act(() => result.current.start());
    expect(createInvestigation).toHaveBeenCalledWith(CONVERSATION, MESSAGE, "key_one");
    expect(begin).toHaveBeenCalledTimes(1);
    expect(result.current.availability).toBe("loaded");
    expect(result.current.investigation?.id).toBe("inv_one");
  });

  it("keeps an uncertain create locked after the immediate 404 and never repeats POST", async () => {
    vi.mocked(getInvestigationByMessage)
      .mockRejectedValueOnce(notFound())
      .mockRejectedValueOnce(notFound())
      .mockRejectedValueOnce(notFound());
    vi.mocked(createInvestigation).mockRejectedValue(network());
    const { result } = follow();
    await waitFor(() => expect(result.current.availability).toBe("available"));
    await act(() => result.current.start());
    expect(result.current.availability).toBe("unconfirmed");
    expect(result.current.unconfirmed).toBe("create");
    expect(createInvestigation).toHaveBeenCalledTimes(1);
    await act(() => result.current.checkServerState());
    expect(result.current.availability).toBe("available");
    expect(createInvestigation).toHaveBeenCalledTimes(1);
  });

  it("discards a late discovery response after the workspace and proposal change", async () => {
    const old = deferred<Investigation>();
    vi.mocked(getInvestigationByMessage)
      .mockReturnValueOnce(old.promise)
      .mockRejectedValueOnce(notFound());
    const view = follow();
    view.rerender({
      workspaceId: "ws_two",
      conversationId: "conv_two",
      proposalMessageId: "msg_two",
    });
    await waitFor(() => expect(view.result.current.availability).toBe("available"));
    act(() => old.resolve(investigation()));
    await act(() => Promise.resolve());
    expect(view.result.current.investigation).toBeNull();
    expect(view.result.current.availability).toBe("available");
  });
});

describe("investigation polling and decisions", () => {
  it("polls active lifecycle state and stops at plan approval", async () => {
    vi.useFakeTimers();
    vi.mocked(getInvestigationByMessage).mockResolvedValue(investigation());
    vi.mocked(getInvestigation).mockResolvedValue(pendingPlan());
    const { result } = follow();
    await act(async () => Promise.resolve());
    expect(result.current.investigation?.status).toBe("planning");
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(result.current.investigation?.status).toBe("awaiting_plan_approval");
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getInvestigation).toHaveBeenCalledTimes(1);
  });

  it("locks plan controls when a failed decision cannot be distinguished from the old state", async () => {
    vi.mocked(getInvestigationByMessage).mockResolvedValue(pendingPlan());
    vi.mocked(decideInvestigationPlan).mockRejectedValue(network());
    vi.mocked(getInvestigation).mockResolvedValue(pendingPlan());
    const { result } = follow();
    await waitFor(() => expect(result.current.investigation?.status).toBe("awaiting_plan_approval"));
    await act(() => result.current.decidePlan("approve"));
    expect(decideInvestigationPlan).toHaveBeenCalledTimes(1);
    expect(result.current.availability).toBe("unconfirmed");
    expect(result.current.unconfirmed).toBe("plan");
    await act(() => result.current.checkServerState());
    expect(result.current.availability).toBe("unconfirmed");
    expect(result.current.unconfirmed).toBe("plan");
  });

  it("treats a validation refusal as definite and does not invent an uncertain transition", async () => {
    vi.mocked(getInvestigationByMessage).mockResolvedValue(pendingPlan());
    vi.mocked(decideInvestigationPlan).mockRejectedValue(
      new ApiClientError("invalid", "request_invalid", null, 422),
    );
    const { result } = follow();
    await waitFor(() => expect(result.current.investigation?.status).toBe("awaiting_plan_approval"));
    await act(() => result.current.decidePlan("approve"));
    expect(result.current.availability).toBe("loaded");
    expect(result.current.unconfirmed).toBeNull();
    expect(result.current.error?.status).toBe(422);
    expect(getInvestigation).not.toHaveBeenCalled();
  });

  it("adopts authoritative state when reconciliation proves the decision happened", async () => {
    const before = pendingPlan();
    vi.mocked(getInvestigationByMessage).mockResolvedValue(before);
    vi.mocked(decideInvestigationPlan).mockRejectedValue(network());
    vi.mocked(getInvestigation).mockResolvedValue(
      investigation({ status: "queued", updated_at: "2026-10-30T10:01:00Z" }),
    );
    const { result } = follow();
    await waitFor(() => expect(result.current.investigation?.status).toBe("awaiting_plan_approval"));
    await act(() => result.current.decidePlan("approve"));
    expect(result.current.availability).toBe("loaded");
    expect(result.current.investigation?.status).toBe("queued");
    expect(result.current.unconfirmed).toBeNull();
  });

  it("keeps SQL only for the exact authorized wave and clears it on scope change", async () => {
    const waiting = investigation({
      status: "awaiting_sql_approval",
      sql_wave: {
        wave_id: "1".repeat(32),
        wave_digest: "a".repeat(64),
        sequence: 1,
        deadline: "2026-10-30T11:00:00Z",
        items: [
          {
            task_id: "trend",
            attempt: 1,
            sql_fingerprint: "b".repeat(64),
            policy_status: "not_applicable",
          },
        ],
      },
    });
    vi.mocked(getInvestigationByMessage)
      .mockResolvedValueOnce(waiting)
      .mockRejectedValueOnce(notFound());
    vi.mocked(getInvestigationSqlWave).mockResolvedValue({
      wave_id: waiting.sql_wave!.wave_id,
      wave_digest: waiting.sql_wave!.wave_digest,
      sequence: 1,
      deadline: waiting.sql_wave!.deadline,
      items: [
        {
          task_id: "trend",
          attempt: 1,
          sql_fingerprint: "b".repeat(64),
          policy_status: "not_applicable",
          sql: "SELECT 1",
        },
      ],
    });
    const view = follow();
    await waitFor(() => expect(view.result.current.sqlWave?.items[0]?.sql).toBe("SELECT 1"));
    view.rerender({
      workspaceId: "ws_two",
      conversationId: "conv_two",
      proposalMessageId: "msg_two",
    });
    expect(view.result.current.sqlWave).toBeNull();
    await waitFor(() => expect(view.result.current.availability).toBe("available"));
  });
});

describe("investigation cancellation", () => {
  it("keeps cancellation requested until a durable read confirms cancellation", async () => {
    vi.useFakeTimers();
    const running = investigation({ status: "running", updated_at: "2026-10-30T10:01:00Z" });
    vi.mocked(getInvestigationByMessage).mockResolvedValue(running);
    vi.mocked(cancelInvestigation).mockResolvedValue(running);
    vi.mocked(getInvestigation).mockResolvedValue(
      investigation({
        status: "cancelled",
        updated_at: "2026-10-30T10:02:00Z",
        explanation: "Cancellation completed.",
      }),
    );
    const { result } = follow();
    await act(async () => Promise.resolve());

    await act(() => result.current.cancel());
    expect(result.current.cancellation).toBe("requested");
    expect(result.current.command).toBe("cancel_requested");
    expect(result.current.investigation?.status).toBe("running");

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(result.current.cancellation).toBe("confirmed");
    expect(result.current.command).toBeNull();
    expect(result.current.investigation?.status).toBe("cancelled");
  });

  it("reports that terminal work finished before cancellation took effect and stops polling", async () => {
    const running = investigation({ status: "running", updated_at: "2026-10-30T10:01:00Z" });
    vi.mocked(getInvestigationByMessage).mockResolvedValue(running);
    vi.mocked(cancelInvestigation).mockResolvedValue(
      investigation({
        status: "completed",
        updated_at: "2026-10-30T10:02:00Z",
        explanation: "The investigation completed before cancellation took effect.",
      }),
    );
    const { result } = follow();
    await waitFor(() => expect(result.current.investigation?.status).toBe("running"));

    await act(() => result.current.cancel());
    expect(result.current.cancellation).toBe("too_late");
    expect(result.current.command).toBeNull();
    expect(result.current.investigation?.status).toBe("completed");
    expect(getInvestigation).not.toHaveBeenCalled();
  });

  it("recovers a proven cancellation after a retryable command failure", async () => {
    const running = investigation({ status: "running", updated_at: "2026-10-30T10:01:00Z" });
    vi.mocked(getInvestigationByMessage).mockResolvedValue(running);
    vi.mocked(cancelInvestigation).mockRejectedValue(network());
    vi.mocked(getInvestigation).mockResolvedValue(
      investigation({ status: "cancelled", updated_at: "2026-10-30T10:02:00Z" }),
    );
    const { result } = follow();
    await waitFor(() => expect(result.current.investigation?.status).toBe("running"));

    await act(() => result.current.cancel());
    expect(result.current.availability).toBe("loaded");
    expect(result.current.cancellation).toBe("confirmed");
    expect(result.current.investigation?.status).toBe("cancelled");
    expect(result.current.unconfirmed).toBeNull();
  });
});

describe("investigation SQL waves", () => {
  it("submits the exact reviewed wave and clears SQL before the request resolves", async () => {
    const waiting = waitingForSql();
    const review = {
      ...waiting.sql_wave!,
      items: waiting.sql_wave!.items.map((item) => ({ ...item, sql: "SELECT month FROM uploaded_data" })),
    };
    const submitted = deferred<Investigation>();
    vi.mocked(getInvestigationByMessage).mockResolvedValue(waiting);
    vi.mocked(getInvestigationSqlWave).mockResolvedValue(review);
    vi.mocked(decideInvestigationSqlWave).mockReturnValue(submitted.promise);
    const { result } = follow();
    await waitFor(() => expect(result.current.sqlWave?.items[0]?.sql).toContain("SELECT"));
    const decision = {
      wave_id: review.wave_id,
      wave_digest: review.wave_digest,
      items: [
        {
          task_id: review.items[0]!.task_id,
          sql_fingerprint: review.items[0]!.sql_fingerprint,
          decision: "approve" as const,
        },
      ],
    };

    let request!: Promise<void>;
    act(() => {
      request = result.current.submitSqlWave(decision);
    });
    expect(result.current.sqlWave).toBeNull();
    expect(decideInvestigationSqlWave).toHaveBeenCalledWith("inv_one", decision);
    // The in-flight command must not fetch the statements again.
    await act(async () => {});
    expect(getInvestigationSqlWave).toHaveBeenCalledTimes(1);
    expect(result.current.sqlWave).toBeNull();
    submitted.resolve(investigation({ status: "queued", updated_at: "2026-10-30T10:01:00Z" }));
    await act(() => request);
    expect(result.current.investigation?.status).toBe("queued");
  });

  it("does not fetch the statements again while cancellation of a paused wave is in flight", async () => {
    const waiting = waitingForSql();
    const cancelled = deferred<Investigation>();
    vi.mocked(getInvestigationByMessage).mockResolvedValue(waiting);
    vi.mocked(getInvestigationSqlWave).mockResolvedValue({
      ...waiting.sql_wave!,
      items: waiting.sql_wave!.items.map((item) => ({ ...item, sql: "SELECT month FROM uploaded_data" })),
    });
    vi.mocked(cancelInvestigation).mockReturnValue(cancelled.promise);
    const { result } = follow();
    await waitFor(() => expect(result.current.sqlWave).not.toBeNull());

    let request!: Promise<void>;
    act(() => {
      request = result.current.cancel();
    });
    await act(async () => {});
    expect(result.current.sqlWave).toBeNull();
    expect(getInvestigationSqlWave).toHaveBeenCalledTimes(1);
    cancelled.resolve(investigation({ status: "cancelled", updated_at: "2026-10-30T10:01:00Z" }));
    await act(() => request);
    expect(result.current.cancellation).toBe("confirmed");
  });

  it("refuses a review whose item binding differs from the durable wave summary", async () => {
    const waiting = waitingForSql();
    vi.mocked(getInvestigationByMessage).mockResolvedValue(waiting);
    vi.mocked(getInvestigationSqlWave).mockResolvedValue({
      ...waiting.sql_wave!,
      items: waiting.sql_wave!.items.map((item) => ({
        ...item,
        sql_fingerprint: "c".repeat(64),
        sql: "SELECT month FROM uploaded_data",
      })),
    });
    const { result } = follow();

    await waitFor(() => expect(result.current.error?.code).toBe("sql_wave_review_mismatch"));
    expect(result.current.sqlWave).toBeNull();
    expect(decideInvestigationSqlWave).not.toHaveBeenCalled();
  });

  it("adopts server-confirmed expiry after a stale wave conflict", async () => {
    const waiting = waitingForSql();
    const review = {
      ...waiting.sql_wave!,
      items: waiting.sql_wave!.items.map((item) => ({ ...item, sql: "SELECT month FROM uploaded_data" })),
    };
    vi.mocked(getInvestigationByMessage).mockResolvedValue(waiting);
    vi.mocked(getInvestigationSqlWave).mockResolvedValue(review);
    vi.mocked(decideInvestigationSqlWave).mockRejectedValue(
      new ApiClientError("The SQL approval expired.", "approval_expired", null, 409),
    );
    vi.mocked(getInvestigation).mockResolvedValue(
      investigation({
        status: "expired",
        updated_at: "2026-10-30T11:00:00Z",
        explanation: "The SQL approval deadline expired.",
      }),
    );
    const { result } = follow();
    await waitFor(() => expect(result.current.sqlWave).not.toBeNull());

    await act(() => result.current.submitSqlWave({
      wave_id: review.wave_id,
      wave_digest: review.wave_digest,
      items: [{
        task_id: review.items[0]!.task_id,
        sql_fingerprint: review.items[0]!.sql_fingerprint,
        decision: "reject",
      }],
    }));

    expect(result.current.availability).toBe("loaded");
    expect(result.current.investigation?.status).toBe("expired");
    expect(result.current.sqlWave).toBeNull();
    expect(result.current.unconfirmed).toBeNull();
  });

  it("clears SQL and locks decisions when a retryable submission remains unconfirmed", async () => {
    const waiting = waitingForSql();
    const review = {
      ...waiting.sql_wave!,
      items: waiting.sql_wave!.items.map((item) => ({ ...item, sql: "SELECT month FROM uploaded_data" })),
    };
    vi.mocked(getInvestigationByMessage).mockResolvedValue(waiting);
    vi.mocked(getInvestigationSqlWave).mockResolvedValue(review);
    vi.mocked(decideInvestigationSqlWave).mockRejectedValue(network());
    vi.mocked(getInvestigation).mockResolvedValue(waiting);
    const { result } = follow();
    await waitFor(() => expect(result.current.sqlWave).not.toBeNull());

    await act(() => result.current.submitSqlWave({
      wave_id: review.wave_id,
      wave_digest: review.wave_digest,
      items: [{
        task_id: review.items[0]!.task_id,
        sql_fingerprint: review.items[0]!.sql_fingerprint,
        decision: "approve",
      }],
    }));

    expect(result.current.availability).toBe("unconfirmed");
    expect(result.current.unconfirmed).toBe("sql_wave");
    expect(result.current.sqlWave).toBeNull();
    expect(decideInvestigationSqlWave).toHaveBeenCalledTimes(1);
  });
});

