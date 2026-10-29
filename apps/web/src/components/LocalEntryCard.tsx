import type { LocalEntry } from "../conversation/thread";

/**
 * The question being sent, or the one that failed. Neither is in the server's history: a failed
 * question was not saved, so this entry disappears on the next question or workspace change.
 */
export function LocalEntryCard({ entry }: { entry: LocalEntry }) {
  const sending = entry.kind === "sending";
  return (
    <div className="local-entry" data-local-entry={entry.kind}>
      <article className="chat-message chat-message--user">
        <p className="message-label">You</p>
        <p>{entry.question}</p>
      </article>
      <article
        className={`chat-message chat-message--assistant chat-message--${entry.kind}`}
        aria-label={sending ? "Answer in progress" : "Question failed"}
        aria-busy={sending || undefined}
      >
        <div className="message-meta">
          <span>Workbench</span>
          <span className={`status-chip status-chip--${entry.kind}`}>
            {sending ? "Sending" : "Failed, not saved"}
          </span>
        </div>
        {entry.kind === "sending" ? (
          <p className="answer-text">
            <span className="progress-indicator" aria-hidden="true" /> Working on your question. This
            can take up to two minutes; leaving or reloading the page does not stop the server.
          </p>
        ) : (
          <>
            <p className="answer-text">{entry.reason}</p>
            <p className="field-hint">
              This question is not in the conversation history. It is back in the question box so
              you can edit it and ask again.
              {entry.requestId ? ` Request ID: ${entry.requestId}.` : ""}
            </p>
          </>
        )}
      </article>
    </div>
  );
}
