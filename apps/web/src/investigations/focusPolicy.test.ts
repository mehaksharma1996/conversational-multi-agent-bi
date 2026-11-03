import { afterEach, describe, expect, it } from "vitest";

import { focusUnlessEditing, isEditing, moveFocusTo } from "./focusPolicy";

function add(tag: string, attributes: Record<string, string> = {}): HTMLElement {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  document.body.append(element);
  return element;
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("background focus moves", () => {
  it("never leaves a control the person is typing in", () => {
    const heading = add("h5", { tabindex: "-1" });
    const question = add("textarea");
    question.focus();
    expect(isEditing()).toBe(true);
    focusUnlessEditing(heading);
    expect(document.activeElement).toBe(question);
  });

  it("moves when focus is on nothing, or on a radio", () => {
    const heading = add("h5", { tabindex: "-1" });
    focusUnlessEditing(heading);
    expect(document.activeElement).toBe(heading);
    const radio = add("input", { type: "radio" });
    radio.focus();
    expect(isEditing()).toBe(false);
    focusUnlessEditing(heading);
    expect(document.activeElement).toBe(heading);
  });
});

describe("requested focus moves", () => {
  it("goes to the anchor, and does nothing for an anchor that is not on the page", () => {
    const target = add("article", { id: "target", tabindex: "-1" });
    moveFocusTo("target");
    expect(document.activeElement).toBe(target);
    moveFocusTo("absent");
    expect(document.activeElement).toBe(target);
  });
});
