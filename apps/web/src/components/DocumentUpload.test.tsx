import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { documentCollectionFixture } from "../test/fixtures";
import { DocumentUpload } from "./DocumentUpload";

const pdf = (name = "policy.pdf") => new File(["%PDF-1.4"], name, { type: "application/pdf" });

function setup(overrides: Partial<Parameters<typeof DocumentUpload>[0]> = {}) {
  const props = {
    disabled: false,
    busy: false,
    collection: null,
    files: [] as File[],
    onFilesChange: vi.fn(),
    onUpload: vi.fn(() => Promise.resolve()),
    onRemove: vi.fn(() => Promise.resolve()),
    ...overrides,
  };
  render(<DocumentUpload {...props} />);
  return props;
}

describe("DocumentUpload (UI-5, #153)", () => {
  it("lists each selected file and removes one without touching the others", async () => {
    const user = userEvent.setup();
    const files = [pdf("a.pdf"), pdf("b.pdf")];
    const props = setup({ files });

    const list = screen.getByRole("list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Remove a.pdf from the selection" }));

    expect(props.onFilesChange).toHaveBeenCalledWith([files[1]]);
  });

  it("refuses a non-PDF selection before anything is sent", () => {
    setup({ files: [pdf(), new File(["x"], "notes.docx")] });

    expect(screen.getByRole("alert")).toHaveTextContent("Only PDF files can be indexed. Remove notes.docx.");
    expect(screen.getByLabelText("PDF documents")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Upload and index PDFs" })).toBeDisabled();
  });

  it("states plainly when nothing is indexed and offers no removal", () => {
    setup();

    expect(screen.getByText(/No documents are indexed/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Remove indexed documents" })).toBeNull();
  });

  it("shows the indexed sources and removes them through the parent", async () => {
    const user = userEvent.setup();
    const props = setup({ collection: documentCollectionFixture });

    const sources = screen.getByRole("region", { name: "Indexed documents" });
    expect(sources).toHaveTextContent("Indexed 1 document(s), 2 page(s), and 3 retrieval chunk(s)");
    expect(within(sources).getByRole("listitem")).toHaveTextContent("policy.pdf");
    await user.click(screen.getByRole("button", { name: "Remove indexed documents" }));

    expect(props.onRemove).toHaveBeenCalledTimes(1);
  });

  it("locks every control while an operation runs", () => {
    setup({ busy: true, files: [pdf()], collection: documentCollectionFixture });

    expect(screen.getByLabelText("PDF documents")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Indexing documents…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove indexed documents" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove policy.pdf from the selection" })).toBeDisabled();
  });
});
