import { focusAsApplication } from "../workspace/focus";

const EDITING =
  'input:not([type="radio"], [type="checkbox"], [type="button"], [type="submit"]), textarea, select, [contenteditable="true"]';

/** True while the person's focus is in a control they type or choose in. */
export function isEditing(): boolean {
  const active = document.activeElement;
  return active instanceof HTMLElement && active.matches(EDITING);
}

/**
 * A background transition (a poll finding a new status) may move focus, but never out of a control
 * the person is typing in: they would lose their place and their next keystrokes.
 */
export function focusUnlessEditing(target: HTMLElement | null | undefined): void {
  if (!isEditing()) focusAsApplication(target);
}

/** A click on a claim or evidence link is the person's own request to move there. */
export function moveFocusTo(anchor: string): void {
  const target = document.getElementById(anchor);
  if (!target) return;
  target.scrollIntoView?.({ block: "nearest" });
  focusAsApplication(target);
}
