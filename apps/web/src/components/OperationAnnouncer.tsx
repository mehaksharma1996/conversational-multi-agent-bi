export interface Announcement {
  text: string;
  /** Increments on every announcement, so repeating the same text is still announced. */
  sequence: number;
}

/**
 * One always-mounted polite live region for operation status. Regions that mount together with
 * their text are missed by some screen readers, so this one exists before anything is announced.
 */
export function OperationAnnouncer({ announcement }: { announcement: Announcement | null }) {
  return (
    <p className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-testid="operation-announcer">
      {announcement ? `${announcement.text}${announcement.sequence % 2 ? " " : ""}` : ""}
    </p>
  );
}
