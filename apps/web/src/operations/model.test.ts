import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  EMPTY_OPERATIONS,
  canTransition,
  isLaneBusy,
  isTerminal,
  laneOf,
  reduceOperations,
  type OperationState,
} from "./model";
import { discardStale, useOperations, type OperationEvent } from "./useOperations";

function start(state: OperationState, id: string, kind: Parameters<typeof laneOf>[0]) {
  return reduceOperations(state, { type: "start", id, kind, startedAt: 0 });
}

describe("operation model", () => {
  it("allows one operation per lane and refuses a second without queueing it", () => {
    const one = start(EMPTY_OPERATIONS, "a", "upload");
    expect(start(one, "b", "analysis")).toBe(one);
    const both = start(one, "c", "download");
    expect(both.map((operation) => operation.id)).toEqual(["a", "c"]);
  });

  it("finishing one operation leaves the other lane busy", () => {
    const both = start(start(EMPTY_OPERATIONS, "q", "conversation"), "d", "download");
    const after = reduceOperations(both, { type: "finish", id: "d", phase: "succeeded" });
    expect(isLaneBusy(after, "mutation")).toBe(true);
    expect(isLaneBusy(after, "read")).toBe(false);
  });

  it("a finish for an unknown or already finished ID changes nothing", () => {
    const one = start(EMPTY_OPERATIONS, "a", "upload");
    expect(reduceOperations(one, { type: "finish", id: "zzz", phase: "failed" })).toBe(one);
    const done = reduceOperations(one, { type: "finish", id: "a", phase: "succeeded" });
    expect(reduceOperations(done, { type: "finish", id: "a", phase: "failed" })).toBe(done);
  });

  it("refuses a reused ID", () => {
    const one = start(EMPTY_OPERATIONS, "a", "upload");
    expect(start(one, "a", "download")).toBe(one);
  });

  it("moves forward only: queued → running → cancelling → terminal", () => {
    let state = reduceOperations(EMPTY_OPERATIONS, {
      type: "start",
      id: "j",
      kind: "analysis",
      startedAt: 0,
      phase: "queued",
    });
    state = reduceOperations(state, { type: "advance", id: "j", phase: "running" });
    state = reduceOperations(state, { type: "advance", id: "j", phase: "cancelling" });
    expect(state[0]?.phase).toBe("cancelling");
    expect(reduceOperations(state, { type: "advance", id: "j", phase: "running" })).toBe(state);
    expect(reduceOperations(state, { type: "finish", id: "j", phase: "cancelled" })).toEqual([]);
  });

  it("records progress without changing the phase", () => {
    const one = start(EMPTY_OPERATIONS, "a", "documents");
    const next = reduceOperations(one, {
      type: "progress",
      id: "a",
      progress: { stage: "indexing", completed: 1, total: 3 },
    });
    expect(next[0]).toMatchObject({ phase: "running", progress: { completed: 1, total: 3 } });
  });

  it("terminal phases never change", () => {
    for (const phase of ["succeeded", "failed", "cancelled", "interrupted"] as const) {
      expect(isTerminal(phase)).toBe(true);
      expect(canTransition(phase, "running")).toBe(false);
    }
    expect(isTerminal("cancelling")).toBe(false);
  });

  it("downloads use the read lane; everything else mutates", () => {
    expect(laneOf("download")).toBe("read");
    expect(laneOf("reset")).toBe("mutation");
    expect(laneOf("approval")).toBe("mutation");
    expect(laneOf("investigation")).toBe("mutation");
  });
});

describe("useOperations", () => {
  it("refuses a second mutation started in the same turn, before React re-renders", () => {
    const { result } = renderHook(() => useOperations());
    let first: ReturnType<typeof result.current.begin> = null;
    let second: ReturnType<typeof result.current.begin> = null;
    act(() => {
      first = result.current.begin("upload");
      second = result.current.begin("conversation");
    });
    expect(first).not.toBeNull();
    expect(second).toBeNull();
    expect(result.current.mutationBusy).toBe(true);
  });

  it("each handle finishes only its own operation and reports events", () => {
    const onEvent = vi.fn<(event: OperationEvent) => void>();
    const { result } = renderHook(() => useOperations({ onEvent }));
    let question: ReturnType<typeof result.current.begin> = null;
    let download: ReturnType<typeof result.current.begin> = null;
    act(() => {
      question = result.current.begin("conversation");
      download = result.current.begin("download");
    });
    act(() => download?.finish("succeeded"));
    expect(result.current.mutationBusy).toBe(true);
    expect(result.current.readBusy).toBe(false);
    expect(result.current.isActive("conversation")).toBe(true);
    act(() => question?.finish("failed"));
    act(() => question?.finish("succeeded"));
    expect(result.current.mutationBusy).toBe(false);
    expect(onEvent.mock.calls.map(([event]) => `${event.kind}:${event.phase}`)).toEqual([
      "conversation:running",
      "download:running",
      "download:succeeded",
      "conversation:failed",
    ]);
  });
});

describe("discardStale", () => {
  it("finishes a stale operation as cancelled, so the lane is free again", () => {
    const onEvent = vi.fn<(event: OperationEvent) => void>();
    const { result } = renderHook(() => useOperations({ onEvent }));
    let upload: ReturnType<typeof result.current.begin> = null;
    act(() => {
      upload = result.current.begin("upload");
    });
    let dropped = false;
    act(() => {
      if (upload) dropped = discardStale(upload, 1, 2);
    });
    expect(dropped).toBe(true);
    expect(result.current.mutationBusy).toBe(false);
    expect(onEvent.mock.calls.map(([event]) => `${event.kind}:${event.phase}`)).toEqual([
      "upload:running",
      "upload:cancelled",
    ]);
  });

  it("leaves a current operation running", () => {
    const { result } = renderHook(() => useOperations());
    let upload: ReturnType<typeof result.current.begin> = null;
    act(() => {
      upload = result.current.begin("upload");
    });
    let dropped = true;
    act(() => {
      if (upload) dropped = discardStale(upload, 3, 3);
    });
    expect(dropped).toBe(false);
    expect(result.current.isActive("upload")).toBe(true);
  });

  it("never touches a newer operation", () => {
    const { result } = renderHook(() => useOperations());
    let old: ReturnType<typeof result.current.begin> = null;
    let newer: ReturnType<typeof result.current.begin> = null;
    act(() => {
      old = result.current.begin("documents");
    });
    act(() => {
      if (old) discardStale(old, 1, 2);
      newer = result.current.begin("upload");
    });
    act(() => {
      if (old) discardStale(old, 1, 2);
    });
    expect(newer).not.toBeNull();
    expect(result.current.isActive("upload")).toBe(true);
    expect(result.current.mutationBusy).toBe(true);
  });
});
