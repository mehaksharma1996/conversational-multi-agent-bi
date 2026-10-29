import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  EMPTY_OPERATIONS,
  activeOperation,
  isLaneBusy,
  reduceOperations,
  type ActivePhase,
  type OperationAction,
  type OperationKind,
  type OperationProgress,
  type OperationState,
  type TerminalPhase,
} from "./model";

/** A started operation. Every call affects only this operation's own record. */
export interface OperationHandle {
  readonly id: string;
  readonly kind: OperationKind;
  advance(phase: ActivePhase): void;
  progress(progress: OperationProgress): void;
  finish(phase: TerminalPhase): void;
}

/**
 * Ends `operation` as cancelled when its response was issued for an earlier workspace (`issuedIn` is
 * the workspace epoch the request started in). Returns true when the caller must drop the response.
 * Only this operation's own record finishes, so a newer operation is never affected.
 */
export function discardStale(operation: OperationHandle, issuedIn: number, current: number): boolean {
  if (issuedIn === current) return false;
  operation.finish("cancelled");
  return true;
}

export interface OperationEvent {
  kind: OperationKind;
  phase: ActivePhase | TerminalPhase;
}

export interface UseOperationsOptions {
  /** Called after every accepted start, advance, and finish (for announcements). */
  onEvent?: (event: OperationEvent) => void;
  now?: () => number;
  newId?: () => string;
}

export function useOperations(options: UseOperationsOptions = {}) {
  const [operations, setOperations] = useState<OperationState>(EMPTY_OPERATIONS);
  // The latest state is mirrored synchronously, so two clicks in the same event loop turn cannot
  // both pass the lane check before React re-renders.
  const current = useRef<OperationState>(EMPTY_OPERATIONS);
  const optionsRef = useRef(options);
  useLayoutEffect(() => {
    optionsRef.current = options;
  });

  const dispatch = useCallback((action: OperationAction): boolean => {
    const next = reduceOperations(current.current, action);
    if (next === current.current) return false;
    current.current = next;
    setOperations(next);
    return true;
  }, []);

  const begin = useCallback(
    (kind: OperationKind, phase: "queued" | "running" = "running"): OperationHandle | null => {
      const { now = Date.now, newId = () => crypto.randomUUID(), onEvent } = optionsRef.current;
      const id = newId();
      if (!dispatch({ type: "start", id, kind, startedAt: now(), phase })) return null;
      onEvent?.({ kind, phase });
      return {
        id,
        kind,
        advance(next) {
          if (dispatch({ type: "advance", id, phase: next })) optionsRef.current.onEvent?.({ kind, phase: next });
        },
        progress(value) {
          dispatch({ type: "progress", id, progress: value });
        },
        finish(terminal) {
          if (dispatch({ type: "finish", id, phase: terminal })) {
            optionsRef.current.onEvent?.({ kind, phase: terminal });
          }
        },
      };
    },
    [dispatch],
  );

  return useMemo(
    () => ({
      operations,
      begin,
      mutationBusy: isLaneBusy(operations, "mutation"),
      readBusy: isLaneBusy(operations, "read"),
      isActive: (kind: OperationKind) => activeOperation(operations, kind) !== null,
    }),
    [operations, begin],
  );
}
