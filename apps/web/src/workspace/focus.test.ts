import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { captureFocusIntent, focusAsApplication, focusOnNextFrame } from "./focus";

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

  it("allows an application-controlled focus transition while rendering", () => {
    const toggle = element("button");
    const link = element("a");
    toggle.focus();
    focusOnNextFrame(() => heading);
    focusAsApplication(link);
    flush();
    expect(heading).toHaveFocus();
  });

  it("allows render focus on a script-only landmark while the destination becomes ready", () => {
    const main = element("main");
    main.tabIndex = -1;
    focusOnNextFrame(() => heading);
    main.focus();
    flush();
    expect(heading).toHaveFocus();
  });

  it("ignores interaction events from a disabled control that cannot claim focus", () => {
    const button = element("button") as HTMLButtonElement;
    button.disabled = true;
    focusOnNextFrame(() => heading);
    button.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    flush();
    expect(heading).toHaveFocus();
  });

  it("leaves focus alone after a user or assistive technology moves it", () => {
    const button = element("button");
    const composer = element("textarea");
    button.focus();
    focusOnNextFrame(() => heading);
    composer.focus();
    flush();
    expect(composer).toHaveFocus();
  });

  it("leaves focus alone when the person moved before an async move became due", () => {
    const button = element("button");
    const composer = element("textarea");
    button.focus();
    const intent = captureFocusIntent();
    composer.focus();
    focusOnNextFrame(() => heading, intent);
    flush();
    expect(composer).toHaveFocus();
  });

  it("does not interrupt typing before a delayed frame", () => {
    const composer = element("textarea");
    composer.focus();
    focusOnNextFrame(() => heading);
    composer.dispatchEvent(new KeyboardEvent("keydown", { key: "W", bubbles: true }));
    flush();
    expect(composer).toHaveFocus();
  });

  it("does not interrupt pasting before a delayed frame", () => {
    const composer = element("textarea");
    composer.focus();
    focusOnNextFrame(() => heading);
    composer.dispatchEvent(new Event("paste", { bubbles: true }));
    flush();
    expect(composer).toHaveFocus();
  });

  it("does not interrupt a user tabbing to another interactive control", () => {
    const first = element("button");
    const second = element("button");
    first.focus();
    focusOnNextFrame(() => heading);
    first.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    second.focus();
    flush();
    expect(second).toHaveFocus();
  });

  it("only runs the newest repeated or overtaking transition", () => {
    const first = element("h2");
    const second = element("h2");
    first.tabIndex = -1;
    second.tabIndex = -1;
    focusOnNextFrame(() => first);
    focusOnNextFrame(() => second);
    flush();
    expect(first).not.toHaveFocus();
    expect(second).toHaveFocus();
  });

  it("tolerates a target that no longer exists", () => {
    focusOnNextFrame(() => null);
    expect(flush).not.toThrow();
  });
});
