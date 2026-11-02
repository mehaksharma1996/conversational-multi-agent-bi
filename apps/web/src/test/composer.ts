import { screen, waitFor } from "@testing-library/react";
import { expect } from "vitest";

/** The Ask composer once it accepts input: it stays locked until the server confirms consent. */
export async function readyComposer(): Promise<HTMLElement> {
  const box = await screen.findByLabelText("Business question");
  await waitFor(() => expect(box).toBeEnabled());
  return box;
}
