import { useState } from "react";

import type { Message, Workspace } from "../api/client";
import { StatusBanner } from "./StatusBanner";

interface ConversationPanelProps {
  workspace: Workspace;
  messages: Message[];
  busy: boolean;
  hasContext: boolean;
  onAcceptConsent: () => Promise<void>;
  onAsk: (question: string) => Promise<void>;
  onExport: (message: Message, format: "csv" | "xlsx") => Promise<void>;
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  switch (typeof value) {
    case "number":
      return value.toLocaleString(undefined, { maximumFractionDigits: 3 });
    case "string":
      return value;
    case "boolean":
      return value ? "true" : "false";
    case "bigint":
      return value.toString();
    case "object":
      return JSON.stringify(value);
    default:
      return "—";
  }
}

function ResultTable({ rows }: { rows: Record<string, unknown>[] }) {
  const columns = Array.from(new Set(rows.flatMap((row) => Object.keys(row))));
  return (
    <div className="table-scroll">
      <table>
        <caption>Query result</caption>
        <thead><tr>{columns.map((column) => <th scope="col" key={column}>{column}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>{columns.map((column) => <td key={column}>{displayValue(row[column])}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ConversationPanel({
  workspace,
  messages,
  busy,
  hasContext,
  onAcceptConsent,
  onAsk,
  onExport,
}: ConversationPanelProps) {
  const [question, setQuestion] = useState("");
  const consentBlocked = workspace.consent_required && !workspace.consent_accepted;

  const submit = () => {
    const trimmed = question.trim();
    if (!trimmed) return;
    setQuestion("");
    void onAsk(trimmed);
  };

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
          to Gemini. Accept this notice before model-backed questions are enabled.
          <button className="button button--secondary consent-button" type="button" disabled={busy} onClick={() => void onAcceptConsent()}>
            I understand, enable Gemini
          </button>
        </StatusBanner>
      ) : null}
      {workspace.local_only_mode ? (
        <StatusBanner kind="success">Local-only mode is active. Gemini-backed SQL and RAG routes are disabled.</StatusBanner>
      ) : null}
      {!workspace.gemini_configured && !workspace.local_only_mode ? (
        <StatusBanner kind="info">Gemini is not configured. Deterministic session-memory questions remain available after analysis.</StatusBanner>
      ) : null}

      <div className="message-list" aria-live="polite">
        {messages.length === 0 ? (
          <p className="empty-state">Ask about the analyzed table, uploaded policy, prior findings, or a combination of both.</p>
        ) : null}
        {messages.map((message) => (
          <div className="message-pair" key={message.id}>
            <article className="chat-message chat-message--user">
              <p className="message-label">You</p><p>{message.question}</p>
            </article>
            <article className="chat-message chat-message--assistant">
              <div className="message-meta"><span>Workbench</span><span className="route-badge">{message.route} route</span></div>
              <p className="answer-text">{message.answer}</p>
              {message.sql ? <pre className="sql-block"><code>{message.sql}</code></pre> : null}
              {message.rows && message.rows.length > 0 ? (
                <>
                  <ResultTable rows={message.rows} />
                  <div className="button-row">
                    <button className="button button--secondary" type="button" onClick={() => void onExport(message, "csv")}>Download CSV</button>
                    <button className="button button--secondary" type="button" onClick={() => void onExport(message, "xlsx")}>Download Excel</button>
                  </div>
                </>
              ) : null}
              {message.sources.length > 0 ? (
                <details><summary>Retrieved document sources</summary><ul className="source-list">{message.sources.map((source) => <li key={source.citation}>{source.citation}</li>)}</ul></details>
              ) : null}
            </article>
          </div>
        ))}
      </div>

      <div className="question-composer">
        <label htmlFor="business-question">Business question</label>
        <textarea
          id="business-question"
          rows={3}
          value={question}
          disabled={busy || !hasContext || consentBlocked}
          placeholder="Which uploaded transactions appear to match the escalation policy?"
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <button className="button button--primary" type="button" disabled={busy || !hasContext || consentBlocked || !question.trim()} onClick={submit}>
          {busy ? "Analyzing question…" : "Ask workbench"}
        </button>
      </div>
    </section>
  );
}
