import { useEffect, useId, useRef, type ReactNode } from "react";

import type { Message, Workspace } from "../api/client";
import { QUESTION_MAX_LENGTH, threadEntries, type AskResult, type LocalEntry } from "../conversation/thread";
import type { ApprovalDraft } from "../workspace/drafts";
import { DEMO_QUESTIONS, type DemoQuestion } from "../demo/retailDemo";
import { LocalEntryCard } from "./LocalEntryCard";
import { MessageCard } from "./MessageCard";
import { StatusBanner } from "./StatusBanner";

/** The counter appears once a question is this close to the server's limit. */
const COUNTER_THRESHOLD = QUESTION_MAX_LENGTH - 400;

export interface DemoContext {
  /** The retail demo table is the loaded, schema-confirmed dataset. */
  dataReady: boolean;
  /** The retail policy PDF is indexed. */
  policyReady: boolean;
}

interface ConversationPanelProps {
  workspace: Workspace;
  messages: Message[];
  /** The question being sent or the one that just failed; held in memory only. */
  localEntry?: LocalEntry | null;
  busy: boolean;
  /** Whether a question is being answered; defaults to `busy`. */
  asking?: boolean;
  /** A sent question's outcome is unknown; asking again waits for a server check. */
  questionUnconfirmed?: boolean;
  onRecheckQuestion?: () => void;
  /** The message whose SQL decision may have reached the server; deciding again waits for a check. */
  approvalUnconfirmed?: string | null;
  onRecheckApproval?: () => void;
  hasContext: boolean;
  demo?: DemoContext;
  /** Composer and approval drafts held by the parent, so they survive the section being unmounted. */
  question: string;
  requireSqlApproval: boolean;
  approvalDrafts: Record<string, ApprovalDraft>;
  onQuestionChange: (update: string | ((current: string) => string)) => void;
  onRequireSqlApprovalChange: (required: boolean) => void;
  onApprovalDraftChange: (messageId: string, draft: ApprovalDraft) => void;
  onAcceptConsent: () => Promise<void>;
  /** Resolves "restore" when the question failed, so it can be put back for editing. */
  onAsk: (question: string, requireSqlApproval: boolean) => Promise<AskResult>;
  onApproval: (message: Message, decision: "approve" | "reject", sql?: string) => Promise<void>;
  onExport: (message: Message, format: "csv" | "xlsx") => Promise<void>;
  investigationMessageId?: string | null;
  investigationPanel?: ReactNode;
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
  localEntry = null,
  busy,
  asking = busy,
  questionUnconfirmed = false,
  onRecheckQuestion,
  approvalUnconfirmed = null,
  onRecheckApproval,
  hasContext,
  demo,
  question,
  requireSqlApproval,
  approvalDrafts,
  onQuestionChange: setQuestion,
  onRequireSqlApprovalChange: setRequireSqlApproval,
  onApprovalDraftChange,
  onAcceptConsent,
  onAsk,
  onApproval,
  onExport,
  investigationMessageId = null,
  investigationPanel = null,
}: ConversationPanelProps) {
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const reasonId = useId();
  const keysId = useId();
  const counterId = useId();
  const consentBlocked = workspace.consent_required && !workspace.consent_accepted;
  const approvalPending = messages.some((message) => message.status === "pending_approval");
  const composerDisabled = busy || !hasContext || consentBlocked || approvalPending || questionUnconfirmed;
  // Outcomes are announced by the shell's single live region; this panel only says why it is locked.
  const disabledReason = questionUnconfirmed
    ? "Asking again is paused until the server confirms whether the last question was answered."
    : asking
      ? "Your question is being answered. This can take up to two minutes."
      : approvalPending
        ? "Resolve the pending SQL approval before asking another question."
        : consentBlocked
          ? "Accept the data-sharing notice above to enable questions."
          : !hasContext
            ? "Add a confirmed table or index documents before asking a question."
            : busy
              ? "Another workbench operation is running. Questions are available when it finishes."
              : null;
  const showCounter = question.length >= COUNTER_THRESHOLD;
  const latest = messages[messages.length - 1];

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
    void onAsk(trimmed, requireSqlApproval).then((result) => {
      if (result === "restore") setQuestion((current) => current || trimmed);
    });
  };

  // Named from server configuration; older API responses without the field default to Gemini.
  const recipients = workspace.data_recipients?.length ? workspace.data_recipients : ["Gemini"];
  const recipientNames = recipients.join(" and ");

  return (
    <section className="panel conversation-panel" aria-labelledby="conversation-heading">
      <div className="section-heading">
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

      {questionUnconfirmed ? (
        <StatusBanner kind="error">
          <strong>The server did not confirm whether your last question was answered.</strong> It is
          not sent again automatically, so asking is paused until the server&apos;s state is checked.
          <button className="button button--link" type="button" disabled={asking} onClick={onRecheckQuestion}>
            Check server state
          </button>
        </StatusBanner>
      ) : null}

      {messages.length === 0 && !localEntry ? (
        <p className="empty-state">
          No questions yet. Ask about the analyzed table, uploaded policy, prior findings, or a
          combination of both.
        </p>
      ) : (
        // Server order (oldest first), then the client-only entry. Each exchange has a heading, so
        // screen-reader heading navigation moves between questions.
        <ol className="message-list" aria-label="Conversation history">
          {threadEntries(messages, localEntry).map((entry) =>
            entry.kind === "message" ? (
              <li key={entry.message.id}>
                <MessageCard
                  message={entry.message}
                  position={entry.position}
                  total={entry.total}
                  busy={busy}
                  decisionUnconfirmed={approvalUnconfirmed === entry.message.id}
                  onRecheckDecision={onRecheckApproval}
                  approvalDraft={approvalDrafts[entry.message.id] ?? null}
                  onApprovalDraftChange={(draft) => onApprovalDraftChange(entry.message.id, draft)}
                  onApproval={onApproval}
                  onExport={onExport}
                  investigationPanel={
                    entry.message.id === investigationMessageId ? investigationPanel : null
                  }
                />
              </li>
            ) : (
              <li key="local-entry">
                <LocalEntryCard entry={entry} />
              </li>
            ),
          )}
        </ol>
      )}

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
          maxLength={QUESTION_MAX_LENGTH}
          disabled={composerDisabled}
          aria-describedby={[disabledReason ? reasonId : null, keysId, showCounter ? counterId : null]
            .filter(Boolean)
            .join(" ")}
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
        {disabledReason ? (
          <p id={reasonId} className="field-hint composer-reason">
            {disabledReason}
          </p>
        ) : null}
        <p id={keysId} className="field-hint">
          Enter sends the question; Shift+Enter adds a new line. A question is never sent twice
          automatically.
        </p>
        {showCounter ? (
          <p id={counterId} className="field-hint">
            {question.length.toLocaleString()} of {QUESTION_MAX_LENGTH.toLocaleString()} characters
          </p>
        ) : null}
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
          {asking ? "Analyzing question…" : "Ask workbench"}
        </button>
      </div>
    </section>
  );
}
