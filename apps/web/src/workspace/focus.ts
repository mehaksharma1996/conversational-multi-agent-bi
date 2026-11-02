/**
 * Moves focus on the next frame, unless the person has since moved it themselves. A frame can arrive
 * late (a loaded device, a throttled tab), and taking focus then would send what they are typing
 * somewhere else, so a question could be submitted cut short. `claimed` is where focus was when the
 * move became due; the default is now. An element only script can focus, such as a heading or the
 * main landmark, is not somewhere a person chose to work, so it never counts as moving on.
 */
export function focusOnNextFrame(
  target: () => HTMLElement | null | undefined,
  claimed: Element | null = document.activeElement,
): void {
  window.requestAnimationFrame(() => {
    const active = document.activeElement;
    const movedOn = active instanceof HTMLElement && active !== claimed && active.tabIndex >= 0;
    if (!movedOn) target()?.focus();
  });
}
