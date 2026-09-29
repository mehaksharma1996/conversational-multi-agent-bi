import type { Message } from "../api/client";
import { criteriaNote, describeRoute, groundingNote, type Note } from "./provenanceNotes";

export function AnswerProvenance({ message }: { message: Message }) {
  const notes = [groundingNote(message), criteriaNote(message)].filter(
    (note): note is Note => note !== null,
  );
  const needsReview = message.route !== "memory" && message.route !== "unsupported";
  return (
    <div className="provenance" role="group" aria-label="How this answer was produced">
      <p className="provenance-route">{describeRoute(message.route)}</p>
      {notes.map((note) => (
        <p key={note.text} className={`provenance-note provenance-note--${note.tone}`}>
          {note.text}
        </p>
      ))}
      {needsReview ? (
        <p className="provenance-review">
          Decision support only: a person should review the evidence before acting.
        </p>
      ) : null}
      <small className="provenance-id">Request ID: {message.request_id}</small>
    </div>
  );
}
