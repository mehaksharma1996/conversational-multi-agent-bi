import { useEffect, useRef, type ReactNode } from "react";

import type { Investigation, InvestigationSqlWave, Message } from "../api/client";
import {
  INVESTIGATION_STATUS_LABEL,
  describePlanningCost,
  isTerminalInvestigation,
  needsPlanDecision,
} from "../investigations/model";
import type {
  InvestigationAvailability,
  InvestigationCancellation,
  InvestigationCommand,
  InvestigationUnconfirmed,
} from "../investigations/useInvestigation";
import type { JobView } from "../operations/useJob";
import { JobProgress } from "./JobProgress";

type Proposal = NonNullable<Message["proposal"]>;

interface InvestigationPanelProps {
  proposal: Proposal;
  availability: InvestigationAvailability;
  investigation: Investigation | null;
  sqlWave: InvestigationSqlWave | null;
  command: InvestigationCommand;
  unconfirmed: InvestigationUnconfirmed | null;
  cancellation: InvestigationCancellation;
  error: Error | null;
  job: JobView;
  busy: boolean;
  onStart: () => void;
  onApprove: () => void;
  onReject: () => void;
  onCancel: () => void;
  onCheckServerState: () => void;
  onRefreshJob: () => void;
}

interface DisplayTask {
  id: string;
  kind: string;
  assignee: string;
  dependsOn: string[];
  successCriterion: string | null;
}

interface DisplayHypothesis {
  id: string;
  statement: string;
  testability: string | null;
  tests: string[];
}

function Notice({ kind, children }: { kind: "info" | "error" | "success"; children: ReactNode }) {
  return <div className={`status-banner status-banner--${kind}`}><div>{children}</div></div>;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function planDetails(plan: Investigation["plan"]): {
  objective: string | null;
  subQuestions: string[];
  tasks: DisplayTask[];
  hypotheses: DisplayHypothesis[];
} {
  const value = record(plan);
  if (!value) return { objective: null, subQuestions: [], tasks: [], hypotheses: [] };
  const tasks = Array.isArray(value.tasks)
    ? value.tasks.flatMap((raw): DisplayTask[] => {
        const item = record(raw);
        if (!item || typeof item.id !== "string") return [];
        return [
          {
            id: item.id,
            kind: typeof item.kind === "string" ? item.kind : "task",
            assignee: typeof item.assignee === "string" ? item.assignee : "unavailable",
            dependsOn: strings(item.depends_on),
            successCriterion:
              typeof item.success_criterion === "string" ? item.success_criterion : null,
          },
        ];
      })
    : [];
  const hypotheses = Array.isArray(value.hypotheses)
    ? value.hypotheses.flatMap((raw): DisplayHypothesis[] => {
        const item = record(raw);
        if (!item || typeof item.id !== "string" || typeof item.statement !== "string") return [];
        return [
          {
            id: item.id,
            statement: item.statement,
            testability: typeof item.testability === "string" ? item.testability : null,
            tests: strings(item.tests),
          },
        ];
      })
    : [];
  return {
    objective: typeof value.objective === "string" ? value.objective : null,
    subQuestions: strings(value.sub_questions),
    tasks,
    hypotheses,
  };
}

function readableToken(value: string): string {
  return value.replaceAll("_", " ");
}

function taskCount(value: number, noun: string): string {
  return `${value} ${noun}${value === 1 ? "" : "s"}`;
}

function deadline(iso: string): string {
  const value = new Date(iso);
  return Number.isNaN(value.getTime()) ? "Server deadline unavailable" : value.toLocaleString();
}

export function InvestigationPanel({
  proposal,
  availability,
  investigation,
  command,
  unconfirmed,
  cancellation,
  error,
  job,
  busy,
  onStart,
  onApprove,
  onReject,
  onCancel,
  onCheckServerState,
  onRefreshJob,
}: InvestigationPanelProps) {
  const planHeading = useRef<HTMLHeadingElement>(null);
  const statusHeading = useRef<HTMLHeadingElement>(null);
  const previousStatus = useRef(investigation?.status ?? null);
  const status = investigation?.status ?? null;

  useEffect(() => {
    const previous = previousStatus.current;
    if (previous === "planning" && status === "awaiting_plan_approval") {
      planHeading.current?.focus();
    } else if (previous !== null && status !== null && isTerminalInvestigation(status)) {
      statusHeading.current?.focus();
    }
    previousStatus.current = status;
  }, [status]);

  if (availability === "idle") return null;

  if (availability === "discovering") {
    return (
      <div className="investigation-workflow" aria-busy="true">
        <p>Checking whether this proposal already has an investigation...</p>
      </div>
    );
  }

  if (availability === "available") {
    return (
      <div className="investigation-workflow">
        <p>
          This proposal has not started an investigation. Starting authorizes planning only; it
          does not approve or execute the resulting plan.
        </p>
        {error ? <Notice kind="error">{error.message}</Notice> : null}
        <button
          className="button button--primary"
          type="button"
          disabled={busy || command !== null}
          onClick={onStart}
        >
          {command === "start" ? "Starting investigation..." : "Start investigation"}
        </button>
      </div>
    );
  }

  if (availability === "unconfirmed") {
    return (
      <div className="investigation-workflow">
        <Notice kind="error">
          <strong>The server has not confirmed the investigation command.</strong> Nothing is sent
          again automatically. Consequential controls stay locked until authoritative state is
          available.
          <button className="button button--link" type="button" onClick={onCheckServerState}>
            Check server state
          </button>
        </Notice>
      </div>
    );
  }

  if (availability === "unavailable" || investigation === null) {
    return (
      <div className="investigation-workflow">
        <Notice kind="error">
          <strong>Investigation state is unavailable.</strong>{" "}
          {error?.message ?? "Reload or request access before taking another action."}
        </Notice>
      </div>
    );
  }

  const plan = planDetails(investigation.plan);
  const review = investigation.proposal;
  const decisionReady = needsPlanDecision(investigation);
  const active = !isTerminalInvestigation(investigation.status);
  const actionBusy = busy || command !== null || unconfirmed !== null;
  const progress = investigation.progress;
  const executionStatus = new Set(["queued", "running", "awaiting_sql_approval"]).has(
    investigation.status,
  );

  return (
    <div className="investigation-workflow" aria-busy={command !== null || undefined}>
      <div className="investigation-status-row">
        <h5 ref={statusHeading} tabIndex={-1}>
          Investigation status
        </h5>
        <span className={`status-chip status-chip--investigation-${investigation.status}`}>
          {INVESTIGATION_STATUS_LABEL[investigation.status]}
        </span>
      </div>

      {error ? <Notice kind="error">{error.message}</Notice> : null}

      {investigation.status === "planning" ? (
        <p>The server is preparing a bounded plan. No plan has been approved or executed.</p>
      ) : null}

      <JobProgress label="the investigation job" view={job} onRefresh={onRefreshJob} />

      {job.state === "unknown" && job.reason === "not_found" ? (
        <p className="field-hint">
          The linked process-local job is unavailable. The investigation lifecycle shown here is
          the durable server record; no work was restarted.
        </p>
      ) : null}

      {cancellation === "requested" ? (
        <Notice kind="info">
          <strong>Cancellation requested.</strong> The investigation has not yet reached a terminal
          cancelled state. A call already in flight may finish, and its result will be discarded.
        </Notice>
      ) : cancellation === "confirmed" ? (
        <Notice kind="success">Cancellation is confirmed by the server.</Notice>
      ) : cancellation === "too_late" ? (
        <Notice kind="info">
          {`The investigation reached ${INVESTIGATION_STATUS_LABEL[investigation.status].toLowerCase()} before cancellation took effect.`}
        </Notice>
      ) : null}

      {progress ? (
        <section className="investigation-progress" aria-labelledby={`progress-${investigation.id}`}>
          <h5 id={`progress-${investigation.id}`}>Task-owner progress</h5>
          <p>
            {taskCount(progress.completed_tasks, "task")} completed out of {progress.total_tasks}.
            These counts come from actual guarded task records, not elapsed time or polling.
          </p>
          {progress.total_tasks > 0 ? (
            <progress
              max={progress.total_tasks}
              value={Math.min(progress.completed_tasks, progress.total_tasks)}
              aria-label="Investigation tasks completed"
            />
          ) : null}
          <h6>By approved task owner</h6>
          <dl className="investigation-owner-progress">
            {progress.task_owners.map((owner) => (
              <div key={owner.assignee}>
                <dt>{readableToken(owner.assignee)}</dt>
                <dd>
                  {`${owner.completed_tasks} completed; ${owner.running_tasks} running; ${owner.pending_tasks} pending; ${owner.total_tasks} total`}
                </dd>
              </div>
            ))}
          </dl>
          <h6>Tasks</h6>
          <ul className="investigation-progress-tasks">
            {progress.tasks.map((task) => (
              <li key={task.task_id}>
                <strong>{task.task_id}</strong>
                <span>{readableToken(task.kind)}</span>
                <span>Owner: {readableToken(task.assignee)}</span>
                <span>Status: {readableToken(task.status)}</span>
                <span>{taskCount(task.attempts, "attempt")}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : executionStatus ? (
        <p className="field-hint">
          Detailed task-owner progress is unavailable. No task percentage or agent activity is
          estimated.
        </p>
      ) : null}

      {investigation.status === "awaiting_plan_approval" ? (
        <section className="investigation-plan" aria-labelledby={`plan-${investigation.id}`}>
          <h5 id={`plan-${investigation.id}`} ref={planHeading} tabIndex={-1}>
            Review the proposed investigation plan
          </h5>
          <p>
            Reviewing this plan does not authorize execution. Only the explicit approval action
            below can ask the server to start it.
          </p>
          {plan.objective ? (
            <div>
              <h6>Objective</h6>
              <p>{plan.objective}</p>
            </div>
          ) : null}
          {plan.subQuestions.length > 0 ? (
            <div>
              <h6>Questions the plan will examine</h6>
              <ol>
                {plan.subQuestions.map((question) => <li key={question}>{question}</li>)}
              </ol>
            </div>
          ) : null}
          {plan.tasks.length > 0 ? (
            <div>
              <h6>Planned tasks</h6>
              <ol className="investigation-task-list">
                {plan.tasks.map((task) => (
                  <li key={task.id}>
                    <strong>{task.id}</strong>: {readableToken(task.kind)} by {readableToken(task.assignee)}
                    {task.dependsOn.length > 0 ? `; after ${task.dependsOn.join(", ")}` : "; no dependencies"}
                    {task.successCriterion ? <span>. Success: {task.successCriterion}</span> : null}
                  </li>
                ))}
              </ol>
            </div>
          ) : (
            <p>The server did not return displayable task details. Approval is unavailable.</p>
          )}
          {plan.hypotheses.length > 0 ? (
            <div>
              <h6>Observational hypotheses</h6>
              <ul>
                {plan.hypotheses.map((hypothesis) => (
                  <li key={hypothesis.id}>
                    <strong>{hypothesis.id}</strong>: {hypothesis.statement}
                    {hypothesis.testability ? ` (${readableToken(hypothesis.testability)})` : ""}
                    {hypothesis.tests.length > 0 ? `; tests ${hypothesis.tests.join(", ")}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {review ? (
            <>
              <p className="cost-estimate">{describePlanningCost(review.cost_estimate)}</p>
              <dl className="proposal-facts">
                <div>
                  <dt>Plan decision deadline</dt>
                  <dd><time dateTime={review.plan_expires_at}>{deadline(review.plan_expires_at)}</time></dd>
                </div>
                <div>
                  <dt>Approval requirements</dt>
                  <dd>{review.approval_requirements.map((item) => `${readableToken(item.kind)}: ${readableToken(item.status)}`).join("; ")}</dd>
                </div>
              </dl>
              {review.missing_information.length > 0 ? (
                <div>
                  <h6>Missing information noted by planning</h6>
                  <ul>{review.missing_information.map((item) => <li key={item}>{item}</li>)}</ul>
                </div>
              ) : null}
            </>
          ) : (
            <Notice kind="error">
              Proposal review details are unavailable. This plan cannot be approved from the browser.
            </Notice>
          )}
          <div className="button-row investigation-actions">
            <button className="button button--primary" type="button" disabled={actionBusy || !decisionReady || plan.tasks.length === 0} onClick={onApprove}>
              Approve and run plan
            </button>
            <button className="button button--danger" type="button" disabled={actionBusy || !decisionReady} onClick={onReject}>
              Reject plan
            </button>
          </div>
        </section>
      ) : null}

      {isTerminalInvestigation(investigation.status) ? (
        <div className="investigation-terminal">
          <p>{investigation.explanation ?? `The investigation ended with status ${INVESTIGATION_STATUS_LABEL[investigation.status].toLowerCase()}.`}</p>
          {investigation.status === "completed" || investigation.status === "completed_partial" ? (
            <p>The verified report and evidence experience belongs to UI-6c and is not shown here.</p>
          ) : null}
        </div>
      ) : null}

      {active ? (
        <div className="investigation-cancel">
          <p className="field-hint">
            Cancellation prevents further work. A provider or SQL call already in flight may finish;
            its result will be discarded.
          </p>
          <button className="button button--secondary" type="button" disabled={actionBusy} onClick={onCancel}>
            Cancel investigation
          </button>
        </div>
      ) : null}

      <p className="sr-only">Proposal source: {proposal.request_reference}</p>
    </div>
  );
}

