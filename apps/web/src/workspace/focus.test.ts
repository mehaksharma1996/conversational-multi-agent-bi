import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { focusOnNextFrame } from "./focus";

describe("focusOnNextFrame", () => {
  const frames: FrameRequestCallback[] = [];
  const flush = () => frames.splice(0).forEach((frame) => frame(0));
  const element = (tag: string) => document.body.appendChild(document.createElement(tag));
  let heading: HTMLElement;

  beforeEach(() => {
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((frame) => frames.push(frame));
    heading = element("h2");
    heading.tabIndex = -1;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    frames.length = 0;
    document.body.replaceChildren();
  });

  it("moves focus when nothing else has claimed it", () => {
    focusOnNextFrame(() => heading);
    flush();
    expect(heading).toHaveFocus();
  });

  it("moves focus from the control that asked for the move", () => {
    const button = element("button");
    button.focus();
    focusOnNextFrame(() => heading);
    flush();
    expect(heading).toHaveFocus();
  });

  it("moves focus when the control that asked is gone", () => {
    const button = element("button");
    button.focus();
    focusOnNextFrame(() => heading);
    button.remove();
    flush();
    expect(heading).toHaveFocus();
  });

  it("leaves focus alone when the person moved it before a late frame", () => {
    const button = element("button");
    const composer = element("textarea");
    button.focus();
    focusOnNextFrame(() => heading);
    composer.focus();
    flush();
    expect(composer).toHaveFocus();
  });

  it("leaves focus alone when the person moved it before the move became due", () => {
    const button = element("button");
    const composer = element("textarea");
    button.focus();
    const claimed = document.activeElement;
    composer.focus();
    focusOnNextFrame(() => heading, claimed);
    flush();
    expect(composer).toHaveFocus();
  });

  it("does not count a script-only element, such as the main landmark, as moving on", () => {
    const button = element("button");
    const main = element("main");
    main.tabIndex = -1;
    button.focus();
    focusOnNextFrame(() => heading);
    main.focus();
    flush();
    expect(heading).toHaveFocus();
  });

  it("tolerates a target that no longer exists", () => {
    focusOnNextFrame(() => null);
    expect(flush).not.toThrow();
  });
});
