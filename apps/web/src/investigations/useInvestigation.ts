import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

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
  type InvestigationSqlWave,
  type InvestigationSqlWaveDecision,
} from "../api/client";
import { jitter, useJob } from "../operations/useJob";
import type { OperationHandle } from "../operations/useOperations";
import { isTerminalInvestigation, needsPlanDecision, shouldPollInvestigation } from "./model";

export type InvestigationUnconfirmed = "create" | "plan" | "sql_wave" | "cancel";
export type InvestigationCancellation = "requested" | "confirmed" | "too_late" | null;
export type InvestigationCommand =
  | "start"
  | "approve"
  | "reject"
  | "cancel"
  | "sql_wave"
  | "cancel_requested"
  | "sql_submitted"
  | null;

export type InvestigationAvailability =
  | "idle"
  | "discovering"
  | "available"
  | "loaded"
  | "unconfirmed"
  | "unavailable";

interface ControllerState {
  availability: InvestigationAvailability;
  investigation: Investigation | null;
  sqlWave: InvestigationSqlWave | null;
  command: InvestigationCommand;
  unconfirmed: InvestigationUnconfirmed | null;
  cancellation: InvestigationCancellation;
  error: ApiClientError | null;
}

export interface InvestigationPollingOptions {
  initialDelayMs?: number;
  maxDelayMs?: number;
  maxPolls?: number;
  maxTransientFailures?: number;
  random?: () => number;
}

export interface UseInvestigationOptions {
  workspaceId: string | null;
  conversationId: string | null;
  proposalMessageId: string | null;
  beginCommand: () => OperationHandle | null;
  polling?: InvestigationPollingOptions;
  newIdempotencyKey?: () => string;
}

const DEFAULT_POLLING: Required<InvestigationPollingOptions> = {
  initialDelayMs: 1_000,
  maxDelayMs: 10_000,
  maxPolls: 200,
  maxTransientFailures: 5,
  random: Math.random,
};

function initialState(hasScope: boolean): ControllerState {
  return {
    availability: hasScope ? "discovering" : "idle",
    investigation: null,
    sqlWave: null,
    command: null,
    unconfirmed: null,
    cancellation: null,
    error: null,
  };
}

function normalize(error: unknown): ApiClientError {
  return error instanceof ApiClientError
    ? error
    : new ApiClientError("Something unexpected happened. Check server state before trying again.");
}

function is404(error: unknown): boolean {
  return error instanceof ApiClientError && error.status === 404;
}

function sameBinding(
  value: Investigation,
  conversationId: string,
  messageId: string,
  investigationId?: string,
): boolean {
  return (
    value.conversation_id === conversationId &&
    value.message_id === messageId &&
    (investigationId === undefined || value.id === investigationId)
  );
}

function cancellationAfterCancel(value: Investigation): Exclude<InvestigationCancellation, null> {
  if (value.status === "cancelled") return "confirmed";
  return isTerminalInvestigation(value.status) ? "too_late" : "requested";
}

function sameWaveReview(
  summary: NonNullable<Investigation["sql_wave"]>,
  review: InvestigationSqlWave,
): boolean {
  if (
    review.wave_id !== summary.wave_id ||
    review.wave_digest !== summary.wave_digest ||
    review.sequence !== summary.sequence ||
    review.deadline !== summary.deadline ||
    review.items.length !== summary.items.length
  ) return false;
  const expected = new Map(
    summary.items.map((item) => [`${item.task_id}\u0000${item.sql_fingerprint}`, item]),
  );
  if (expected.size !== summary.items.length) return false;
  for (const item of review.items) {
    const key = `${item.task_id}\u0000${item.sql_fingerprint}`;
    const match = expected.get(key);
    if (match?.attempt !== item.attempt || match.policy_status !== item.policy_status) return false;
    expected.delete(key);
  }
  return expected.size === 0;
}

export function useInvestigation({
  workspaceId,
  conversationId,
  proposalMessageId,
  beginCommand,
  polling = {},
  newIdempotencyKey = () => crypto.randomUUID(),
}: UseInvestigationOptions) {
  const hasScope = workspaceId !== null && conversationId !== null && proposalMessageId !== null;
  const scopeKey = `${workspaceId ?? ""}|${conversationId ?? ""}|${proposalMessageId ?? ""}`;
  const scopeRef = useRef(scopeKey);
  useLayoutEffect(() => {
    scopeRef.current = scopeKey;
  }, [scopeKey]);
  const generation = useRef(0);
  const pollingRef = useRef({ ...DEFAULT_POLLING, ...polling });
  const beginRef = useRef(beginCommand);
  const idempotency = useRef<{ scope: string; key: string } | null>(null);
  useEffect(() => {
    pollingRef.current = { ...DEFAULT_POLLING, ...polling };
    beginRef.current = beginCommand;
  });

  const [stored, setStored] = useState<{ key: string; state: ControllerState }>(() => ({
    key: scopeKey,
    state: initialState(hasScope),
  }));
  const state = stored.key === scopeKey ? stored.state : initialState(hasScope);

  const update = useCallback(
    (change: (current: ControllerState) => ControllerState) => {
      const owner = scopeRef.current;
      setStored((current) => ({
        key: owner,
        state: change(current.key === owner ? current.state : initialState(owner.split("|").every(Boolean))),
      }));
    },
    [],
  );

  const adopt = useCallback(
    (
      value: Investigation,
      command: InvestigationCommand = null,
      cancellation?: InvestigationCancellation,
    ) => {
      update((current) => ({
        ...current,
        availability: "loaded",
        investigation: value,
        sqlWave:
          value.status === "awaiting_sql_approval" &&
          current.investigation?.id === value.id &&
          current.investigation.sql_wave?.wave_id === value.sql_wave?.wave_id
            ? current.sqlWave
            : null,
        command,
        unconfirmed: null,
        cancellation: cancellation === undefined ? current.cancellation : cancellation,
        error: null,
      }));
    },
    [update],
  );

  // Read-only discovery is the only automatic recovery action. It never repeats creation.
  useEffect(() => {
    const current = ++generation.current;
    idempotency.current = null;
    if (!hasScope || conversationId === null || proposalMessageId === null) return;
    void getInvestigationByMessage(conversationId, proposalMessageId)
      .then((value) => {
        if (current !== generation.current || !sameBinding(value, conversationId, proposalMessageId)) return;
        adopt(value);
      })
      .catch((caught: unknown) => {
        if (current !== generation.current) return;
        if (is404(caught)) {
          update((previous) => ({ ...previous, availability: "available", error: null }));
          return;
        }
        update((previous) => ({
          ...previous,
          availability: "unavailable",
          error: normalize(caught),
        }));
      });
    return () => {
      generation.current += 1;
      // SQL is intentionally not copied anywhere else; invalidating this scope makes it unreachable.
    };
  }, [scopeKey, hasScope, conversationId, proposalMessageId, adopt, update]);

  const polledInvestigationId = state.investigation?.id ?? null;
  const polledInvestigationStatus = state.investigation?.status ?? null;

  // Bounded, one-at-a-time investigation polling. Decision and terminal states stop naturally.
  useEffect(() => {
    const continueAfterDecision =
      state.command === "cancel_requested" || state.command === "sql_submitted";
    if (
      state.availability !== "loaded" ||
      polledInvestigationId === null ||
      polledInvestigationStatus === null ||
      (!shouldPollInvestigation(polledInvestigationStatus) && !continueAfterDecision) ||
      conversationId === null ||
      proposalMessageId === null
    ) {
      return;
    }
    const current = generation.current;
    const { initialDelayMs, maxDelayMs, maxPolls, maxTransientFailures, random } = pollingRef.current;
    let delay = initialDelayMs;
    let polls = 0;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;
    const schedule = () => {
      timer = setTimeout(() => void poll(), jitter(delay, random));
      delay = Math.min(delay * 2, maxDelayMs);
    };
    const poll = async () => {
      if (stopped || current !== generation.current) return;
      polls += 1;
      try {
        const value = await getInvestigation(polledInvestigationId);
        if (
          stopped ||
          current !== generation.current ||
          !sameBinding(value, conversationId, proposalMessageId, polledInvestigationId)
        ) {
          return;
        }
        failures = 0;
        const cancelFinished =
          state.command === "cancel_requested" && isTerminalInvestigation(value.status);
        const keepCommand =
          (state.command === "cancel_requested" && !cancelFinished) ||
          (state.command === "sql_submitted" && value.status === "awaiting_sql_approval");
        const cancellation =
          state.command === "cancel_requested"
            ? value.status === "cancelled"
              ? "confirmed"
              : cancelFinished
                ? "too_late"
                : "requested"
            : undefined;
        adopt(value, keepCommand ? state.command : null, cancellation);
        if (!shouldPollInvestigation(value.status) && !keepCommand) return;
      } catch (caught) {
        if (stopped || current !== generation.current) return;
        const error = normalize(caught);
        if (!error.retryable) {
          update((previous) => ({ ...previous, availability: "unavailable", error, sqlWave: null }));
          return;
        }
        failures += 1;
        if (failures >= maxTransientFailures) {
          update((previous) => ({ ...previous, availability: "unavailable", error, sqlWave: null }));
          return;
        }
      }
      if (polls >= maxPolls) {
        update((previous) => ({
          ...previous,
          availability: "unavailable",
          error: new ApiClientError(
            "Investigation status could not be confirmed within the polling limit. Check server state.",
            "poll_limit",
          ),
          sqlWave: null,
        }));
        return;
      }
      schedule();
    };
    schedule();
    return () => {
      stopped = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [
    state.availability,
    polledInvestigationId,
    polledInvestigationStatus,
    state.command,
    conversationId,
    proposalMessageId,
    adopt,
    update,
  ]);

  const sqlTarget = state.investigation;

  // SQL exists only in this scoped state, only while the matching authorized wave is pending and
  // no command is in flight: a submission or cancellation must not fetch the statements again.
  useEffect(() => {
    const summary = sqlTarget?.sql_wave;
    if (
      state.availability !== "loaded" ||
      sqlTarget?.status !== "awaiting_sql_approval" ||
      !summary ||
      state.command !== null
    ) {
      update((previous) => previous.sqlWave === null ? previous : { ...previous, sqlWave: null });
      return;
    }
    const current = generation.current;
    let active = true;
    update((previous) => ({ ...previous, sqlWave: null }));
    void getInvestigationSqlWave(sqlTarget.id)
      .then((wave) => {
        if (!active || current !== generation.current) return;
        if (!sameWaveReview(summary, wave)) {
          update((previous) => ({
            ...previous,
            sqlWave: null,
            error: new ApiClientError(
              "The SQL review did not match the pending server wave. Nothing was accepted.",
              "sql_wave_review_mismatch",
            ),
          }));
          return;
        }
        update((previous) => ({ ...previous, sqlWave: wave, error: null }));
      })
      .catch((caught: unknown) => {
        if (!active || current !== generation.current) return;
        update((previous) => ({ ...previous, sqlWave: null, error: normalize(caught) }));
      });
    return () => {
      active = false;
    };
  }, [state.availability, sqlTarget, state.command, update]);

  const job = useJob(state.investigation?.job_id ?? null, workspaceId);

  const markUnconfirmed = useCallback(
    (kind: InvestigationUnconfirmed, error: unknown) => {
      update((current) => ({
        ...current,
        availability: "unconfirmed",
        command: null,
        unconfirmed: kind,
        sqlWave: null,
        error: normalize(error),
      }));
    },
    [update],
  );

  const start = useCallback(async () => {
    if (
      state.availability !== "available" ||
      conversationId === null ||
      proposalMessageId === null
    ) return;
    const operation = beginRef.current();
    if (!operation) return;
    const current = generation.current;
    const entry = idempotency.current;
    const key = entry?.scope === scopeKey ? entry.key : newIdempotencyKey();
    idempotency.current = { scope: scopeKey, key };
    update((previous) => ({ ...previous, command: "start", error: null }));
    try {
      const value = await createInvestigation(conversationId, proposalMessageId, key);
      if (current !== generation.current) return operation.finish("cancelled");
      if (!sameBinding(value, conversationId, proposalMessageId)) {
        markUnconfirmed("create", new ApiClientError("The server returned an unrelated investigation."));
        return operation.finish("interrupted");
      }
      adopt(value);
      operation.finish("succeeded");
    } catch (caught) {
      if (current !== generation.current) return operation.finish("cancelled");
      const error = normalize(caught);
      if (!error.retryable) {
        update((previous) => ({ ...previous, command: null, error }));
        return operation.finish("failed");
      }
      try {
        const value = await getInvestigationByMessage(conversationId, proposalMessageId);
        if (current !== generation.current) return operation.finish("cancelled");
        if (sameBinding(value, conversationId, proposalMessageId)) {
          adopt(value);
          return operation.finish("succeeded");
        }
      } catch {
        // A retryable create followed by 404 is still unknown; the POST may still reach the server.
      }
      markUnconfirmed("create", error);
      operation.finish("interrupted");
    }
  }, [state.availability, conversationId, proposalMessageId, scopeKey, newIdempotencyKey, update, adopt, markUnconfirmed]);

  const reconcileMutation = useCallback(
    async (kind: Exclude<InvestigationUnconfirmed, "create">, before: Investigation, error: unknown) => {
      if (conversationId === null || proposalMessageId === null) return;
      try {
        const value = await getInvestigation(before.id);
        if (!sameBinding(value, conversationId, proposalMessageId, before.id)) {
          markUnconfirmed(kind, error);
          return;
        }
        if (value.status !== before.status || value.updated_at !== before.updated_at) {
          if (kind === "cancel") {
            const cancellation = cancellationAfterCancel(value);
            adopt(value, cancellation === "requested" ? "cancel_requested" : null, cancellation);
          } else {
            adopt(value);
          }
          return;
        }
      } catch {
        // No authoritative transition was read; keep the consequential controls locked.
      }
      markUnconfirmed(kind, error);
    },
    [conversationId, proposalMessageId, adopt, markUnconfirmed],
  );

  const settleCommandFailure = useCallback(
    async (
      kind: Exclude<InvestigationUnconfirmed, "create">,
      before: Investigation,
      caught: unknown,
    ): Promise<"failed" | "interrupted"> => {
      const error = normalize(caught);
      if (!error.retryable && error.status !== 409) {
        const unavailable = error.status === 401 || error.status === 403 || error.status === 404;
        update((previous) => ({
          ...previous,
          availability: unavailable ? "unavailable" : "loaded",
          command: null,
          sqlWave: unavailable ? null : previous.sqlWave,
          error,
        }));
        return "failed";
      }
      await reconcileMutation(kind, before, error);
      return "interrupted";
    },
    [reconcileMutation, update],
  );

  const decidePlan = useCallback(
    async (decision: "approve" | "reject") => {
      const before = state.investigation;
      if (state.availability !== "loaded" || before === null || !needsPlanDecision(before)) return;
      const operation = beginRef.current();
      if (!operation) return;
      const current = generation.current;
      update((previous) => ({ ...previous, command: decision, error: null }));
      try {
        const value = await decideInvestigationPlan(before.id, {
          decision,
          plan_digest: before.plan_digest!,
          authority_epoch: before.authority_epoch!,
          follow_up_rounds: 0,
        });
        if (current !== generation.current) return operation.finish("cancelled");
        if (
          conversationId === null ||
          proposalMessageId === null ||
          !sameBinding(value, conversationId, proposalMessageId, before.id)
        ) {
          markUnconfirmed("plan", new ApiClientError("The server returned an unrelated investigation."));
          return operation.finish("interrupted");
        }
        adopt(value);
        operation.finish("succeeded");
      } catch (caught) {
        if (current !== generation.current) return operation.finish("cancelled");
        operation.finish(await settleCommandFailure("plan", before, caught));
      }
    },
    [state.availability, state.investigation, conversationId, proposalMessageId, update, adopt, markUnconfirmed, settleCommandFailure],
  );

  const cancel = useCallback(async () => {
    const before = state.investigation;
    if (state.availability !== "loaded" || before === null) return;
    const operation = beginRef.current();
    if (!operation) return;
    const current = generation.current;
    update((previous) => ({
      ...previous,
      command: "cancel",
      cancellation: null,
      error: null,
      sqlWave: null,
    }));
    try {
      const value = await cancelInvestigation(before.id);
      if (current !== generation.current) return operation.finish("cancelled");
      if (
        conversationId === null ||
        proposalMessageId === null ||
        !sameBinding(value, conversationId, proposalMessageId, before.id)
      ) {
        markUnconfirmed("cancel", new ApiClientError("The server returned an unrelated investigation."));
        return operation.finish("interrupted");
      }
      const cancellation = cancellationAfterCancel(value);
      adopt(value, cancellation === "requested" ? "cancel_requested" : null, cancellation);
      operation.finish("succeeded");
    } catch (caught) {
      if (current !== generation.current) return operation.finish("cancelled");
        operation.finish(await settleCommandFailure("cancel", before, caught));
    }
  }, [state.availability, state.investigation, conversationId, proposalMessageId, update, adopt, markUnconfirmed, settleCommandFailure]);

  const submitSqlWave = useCallback(
    async (decision: InvestigationSqlWaveDecision) => {
      const before = state.investigation;
      const wave = state.sqlWave;
      if (
        state.availability !== "loaded" ||
        before?.status !== "awaiting_sql_approval" ||
        wave === null ||
        decision.wave_id !== wave.wave_id ||
        decision.wave_digest !== wave.wave_digest
      ) return;
      const operation = beginRef.current();
      if (!operation) return;
      const current = generation.current;
      update((previous) => ({ ...previous, command: "sql_wave", error: null, sqlWave: null }));
      try {
        const value = await decideInvestigationSqlWave(before.id, decision);
        if (current !== generation.current) return operation.finish("cancelled");
        if (
          conversationId === null ||
          proposalMessageId === null ||
          !sameBinding(value, conversationId, proposalMessageId, before.id)
        ) {
          markUnconfirmed("sql_wave", new ApiClientError("The server returned an unrelated investigation."));
          return operation.finish("interrupted");
        }
        adopt(value, value.status === "awaiting_sql_approval" ? "sql_submitted" : null);
        operation.finish("succeeded");
      } catch (caught) {
        if (current !== generation.current) return operation.finish("cancelled");
        operation.finish(await settleCommandFailure("sql_wave", before, caught));
      }
    },
    [state.availability, state.investigation, state.sqlWave, conversationId, proposalMessageId, update, adopt, markUnconfirmed, settleCommandFailure],
  );

  const checkServerState = useCallback(async () => {
    if (state.availability !== "unconfirmed" || conversationId === null || proposalMessageId === null) return;
    const current = generation.current;
    try {
      const value = state.investigation
        ? await getInvestigation(state.investigation.id)
        : await getInvestigationByMessage(conversationId, proposalMessageId);
      if (
        current !== generation.current ||
        !sameBinding(value, conversationId, proposalMessageId, state.investigation?.id)
      ) return;
      if (
        state.unconfirmed !== "create" &&
        state.investigation !== null &&
        value.status === state.investigation.status &&
        value.updated_at === state.investigation.updated_at
      ) {
        markUnconfirmed(
          state.unconfirmed ?? "plan",
          state.error ??
            new ApiClientError(
              "The server still shows the earlier state, so the command outcome remains unknown.",
              "outcome_unconfirmed",
            ),
        );
        return;
      }
      if (state.unconfirmed === "cancel") {
        const cancellation = cancellationAfterCancel(value);
        adopt(value, cancellation === "requested" ? "cancel_requested" : null, cancellation);
      } else {
        adopt(value);
      }
    } catch (caught) {
      if (current !== generation.current) return;
      if (state.unconfirmed === "create" && is404(caught)) {
        // This is an explicit later check, not the immediate post-failure lookup.
        update((previous) => ({
          ...previous,
          availability: "available",
          investigation: null,
          unconfirmed: null,
          error: null,
        }));
        return;
      }
      markUnconfirmed(state.unconfirmed ?? "create", caught);
    }
  }, [state.availability, state.investigation, state.unconfirmed, state.error, conversationId, proposalMessageId, adopt, update, markUnconfirmed]);

  return {
    ...state,
    job: job.view,
    refreshJob: job.refresh,
    start,
    decidePlan,
    cancel,
    submitSqlWave,
    checkServerState,
  };
}

