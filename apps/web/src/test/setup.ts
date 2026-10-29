import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no modal dialogs. This stand-in only toggles `open` and fires `cancel` on Escape; the
// real inert background and focus containment are covered by the Playwright specs.
if (typeof HTMLDialogElement !== "undefined" && !HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false;
  };
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const dialog = document.querySelector<HTMLDialogElement>("dialog[open]");
    dialog?.dispatchEvent(new Event("cancel", { cancelable: true }));
  });
}

afterEach(() => cleanup());
