import { useEffect, useRef, useState } from "react";

import type { Message, Workspace } from "../api/client";
import { DEMO_QUESTIONS, type DemoQuestion } from "../demo/retailDemo";
import { MessageCard } from "./MessageCard";
import { StatusBanner } from "./StatusBanner";

export interface DemoContext {
  /** The retail demo table is the loaded, schema-confirmed dataset. */
  dataReady: boolean;
  /** The retail policy PDF is indexed. */
  policyReady: boolean;
}

interface ConversationPanelProps {
  workspace: Workspace;
  messages: Message[];
  busy: boolean;
  hasContext: boolean;
  demo?: DemoContext;
  onAcceptConsent: () => Promise<void>;
  /** Resolves true when the question was answered, so a failed one can be put back for retry. */
  onAsk: (question: string, requireSqlApproval: boolean) => Promise<boolean>;
  onApproval: (message: Message, decision: "approve" | "reject", sql?: string) => Promise<void>;
  onExport: (message: Message, format: "csv" | "xlsx") => Promise<void>;
}

function sampleAvailable(item: DemoQuestion, demo: DemoContext): boolean {
  if (item.needs === "data") return demo.dataReady;
  if (item.needs === "policy") return demo.policyReady;
  return demo.dataReady && demo.policyReady;
}

function missingReason(item: DemoQuestion): string {
  if (item.needs === "policy") return "Index the policy PDF to enable this question.";
  if (item.needs === "both") return "Needs the demo table and the policy PDF.";
  return "Load and confirm the demo table to enable this question.";
}

export function ConversationPanel({
  workspace,
  messages,
  busy,
  hasContext,
  demo,
  onAcceptConsent,
  onAsk,
  onApproval,
  onExport,
}: ConversationPanelProps) {
  const [question, setQuestion] = useState("");
  const [requireSqlApproval, setRequireSqlApproval] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const consentBlocked = workspace.consent_required && !workspace.consent_accepted;
  const approvalPending = messages.some((message) => message.status === "pending_approval");
  const composerDisabled = busy || !hasContext || consentBlocked || approvalPending;

  // Tell screen-reader users what happened. Derived from the latest message, so nothing is
  // announced for history that was already on screen when the panel first rendered.
  const [initialCount] = useState(messages.length);
  const latest = messages[messages.length - 1];
  let announcement = "";
  if (latest && messages.length > initialCount) {
    if (latest.status === "pending_approval") announcement = "SQL approval required. Nothing has run yet.";
    else if (latest.status === "rejected") announcement = "Request not executed. No query result was kept.";
    else announcement = `Answer ready, ${latest.route} route.`;
  }

  // Move focus to where the next action is: the approval controls when one is requested, and the
  // question box once an approval has been resolved.
  const previousCount = useRef(messages.length);
  const wasPending = useRef(approvalPending);
  useEffect(() => {
    if (messages.length > previousCount.current && latest?.status === "pending_approval") {
      document
        .querySelector<HTMLElement>(`[data-message-id="${latest.id}"] .approval-controls`)
        ?.focus();
    }
    if (wasPending.current && !approvalPending) composerRef.current?.focus();
    previousCount.current = messages.length;
    wasPending.current = approvalPending;
  }, [messages, latest, approvalPending]);

  const submit = () => {
    const trimmed = question.trim();
    if (!trimmed || composerDisabled) return;
    setQuestion("");
    void onAsk(trimmed, requireSqlApproval).then((answered) => {
      if (!answered) setQuestion((current) => current || trimmed);
    });
  };

  // Named from server configuration; older API responses without the field default to Gemini.
  const recipients = workspace.data_recipients?.length ? workspace.data_recipients : ["Gemini"];
  const recipientNames = recipients.join(" and ");

  return (
    <section className="panel conversation-panel" aria-labelledby="conversation-heading">
      <div className="section-heading">
        <span className="step-number" aria-hidden="true">6</span>
        <div>
          <p className="eyebrow">Bounded agent workflows</p>
          <h2 id="conversation-heading">Ask across data and documents</h2>
        </div>
      </div>

      {consentBlocked ? (
        <StatusBanner kind="info">
          Questions may send question text, table schema and samples, or retrieved PDF excerpts
          to {recipientNames}. Accept this notice before model-backed questions are enabled.
          <button
            className="button button--secondary consent-button"
            type="button"
            disabled={busy}
            onClick={() => void onAcceptConsent()}
          >
            I understand, enable {recipientNames}
          </button>
        </StatusBanner>
      ) : null}
      {workspace.local_only_mode ? (
        <StatusBanner kind="success">
          Local-only mode is active. Gemini-backed SQL and RAG routes are disabled.
        </StatusBanner>
      ) : null}
      {!workspace.gemini_configured && !workspace.local_only_mode ? (
        <StatusBanner kind="info">
          Gemini is not configured. Deterministic session-memory questions remain available after
          analysis.
        </StatusBanner>
      ) : null}

      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
      <div className="message-list">
        {messages.length === 0 ? (
          <p className="empty-state">
            Ask about the analyzed table, uploaded policy, prior findings, or a combination of
            both.
          </p>
        ) : null}
        {messages.map((message) => (
          <MessageCard
            key={message.id}
            message={message}
            busy={busy}
            onApproval={onApproval}
            onExport={onExport}
          />
        ))}
      </div>

      {demo && (demo.dataReady || demo.policyReady) ? (
        <section className="sample-questions" aria-labelledby="sample-questions-heading">
          <h3 id="sample-questions-heading">Sample questions for the retail demo</h3>
          <p className="field-hint">
            Each has a committed answer in the demo&apos;s ground-truth file. Selecting one fills
            the question box; you still choose when to send it.
          </p>
          <ul>
            {DEMO_QUESTIONS.map((item) => {
              const available = sampleAvailable(item, demo);
              return (
                <li key={item.groundTruthId}>
                  <button
                    className="sample-question"
                    type="button"
                    disabled={!available || composerDisabled}
                    title={available ? undefined : missingReason(item)}
                    onClick={() => {
                      setQuestion(item.question);
                      composerRef.current?.focus();
                    }}
                  >
                    <span className="sample-category">
                      {item.category} · {item.route}
                    </span>
                    <span>{item.question}</span>
                    {available ? null : (
                      <span className="field-hint">{missingReason(item)}</span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}

      <div className="question-composer">
        <label htmlFor="business-question">Business question</label>
        <textarea
          id="business-question"
          ref={composerRef}
          rows={3}
          value={question}
          disabled={composerDisabled}
          placeholder={
            approvalPending
              ? "Resolve the pending SQL approval before asking another question."
              : "Which uploaded transactions appear to match the escalation policy?"
          }
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <label className="approval-option">
          <input
            type="checkbox"
            checked={requireSqlApproval}
            disabled={composerDisabled}
            onChange={(event) => setRequireSqlApproval(event.target.checked)}
          />
          Require approval before hybrid SQL runs
        </label>
        <button
          className="button button--primary"
          type="button"
          disabled={composerDisabled || !question.trim()}
          onClick={submit}
        >
          {busy ? "Analyzing question…" : "Ask workbench"}
        </button>
      </div>
    </section>
  );
}
