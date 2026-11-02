export interface FocusIntent {
  readonly revision: number;
}

let intentRevision = 0;
let transitionRevision = 0;
let applicationFocusDepth = 0;

// Focus itself is an intent signal, including focus moved through a screen reader. Pointer, keyboard,
// and editing events also invalidate a pending transition before their eventual focus/input effects.
if (typeof document !== "undefined") {
  const recordIntent = () => {
    intentRevision += 1;
  };
  const recordUsableIntent = (event: Event) => {
    const target = event.target;
    if (target instanceof HTMLElement && target.matches(":disabled")) return;
    recordIntent();
  };
  const recordFocusIntent = (event: FocusEvent) => {
    const target = event.target;
    if (
      applicationFocusDepth === 0 &&
      target instanceof HTMLElement &&
      target.tabIndex >= 0
    ) recordIntent();
  };
  document.addEventListener("focusin", recordFocusIntent, true);
  for (const event of ["pointerdown", "keydown", "beforeinput", "paste"]) {
    document.addEventListener(event, recordUsableIntent, true);
  }
}

/** Captures the user-interaction boundary that a later focus transition belongs to. */
export function captureFocusIntent(): FocusIntent {
  return { revision: intentRevision };
}

/** Moves focus while identifying it as application-managed rather than a new user intent. */
export function focusAsApplication(target: HTMLElement | null | undefined): void {
  if (!target) return;
  applicationFocusDepth += 1;
  try {
    target.focus();
  } finally {
    applicationFocusDepth -= 1;
  }
}

/**
 * Moves focus on the next frame if the same user intent still owns the transition. A late frame must
 * not interrupt typing, pasting, Tab navigation, pointer use, or focus on an interactive control.
 * Script-only landmarks/headings and known application-managed focus may change while rendering
 * without cancelling the destination.
 * A newer scheduled transition overtakes an older one, so stale callbacks cannot reclaim focus.
 */
export function focusOnNextFrame(
  target: () => HTMLElement | null | undefined,
  intent: FocusIntent = captureFocusIntent(),
): void {
  const transition = ++transitionRevision;
  window.requestAnimationFrame(() => {
    if (transition !== transitionRevision || intent.revision !== intentRevision) return;
    focusAsApplication(target());
  });
}
