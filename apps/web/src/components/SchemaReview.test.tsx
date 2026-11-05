import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Dataset } from "../api/client";
import { reviewDatasetFixture } from "../test/fixtures";
import { SchemaReview } from "./SchemaReview";

describe("SchemaReview", () => {
  it("requires explicit selection for a low-confidence suggestion", () => {
    const dataset = {
      ...reviewDatasetFixture,
      schema_mapping: {
        ...reviewDatasetFixture.schema_mapping,
        mappings: {
          ...reviewDatasetFixture.schema_mapping.mappings,
          amount: {
            ...reviewDatasetFixture.schema_mapping.mappings.amount,
            confidence: 0.4,
            reason: "Only numeric column available.",
          },
        },
      },
    } satisfies Dataset;

    render(<SchemaReview dataset={dataset} busy={false} draft={null} onDraftChange={vi.fn()} onConfirm={vi.fn()} />);

    expect(screen.getByLabelText("amount")).toHaveValue("");
    expect(screen.getByText("Low-confidence suggestions are not selected automatically", { exact: false })).toBeVisible();
  });

  const withSuggestedLabel = (filename: string): Dataset => ({
    ...reviewDatasetFixture,
    filename,
    schema_mapping: {
      ...reviewDatasetFixture.schema_mapping,
      mappings: {
        ...reviewDatasetFixture.schema_mapping.mappings,
        label: {
          canonical_field: "label",
          source_column: "merchant",
          confidence: 0.95,
          reason: "Column name directly matches a known alias for label.",
        },
      },
    },
  });

  it("leaves the label unmapped for the bundled demo file and says why", () => {
    render(
      <SchemaReview dataset={withSuggestedLabel("retail_operations.csv")} busy={false} draft={null} onDraftChange={vi.fn()} onConfirm={vi.fn()} />,
    );

    expect(screen.getByLabelText("label")).toHaveValue("");
    expect(screen.getByText("label is left unmapped for the demo:")).toBeVisible();
    expect(screen.getByText("not an outcome to predict", { exact: false })).toBeVisible();
  });

  it("keeps a confident label suggestion for any other file", () => {
    render(<SchemaReview dataset={withSuggestedLabel("my_data.csv")} busy={false} draft={null} onDraftChange={vi.fn()} onConfirm={vi.fn()} />);

    expect(screen.getByLabelText("label")).toHaveValue("merchant");
    expect(screen.queryByText("label is left unmapped for the demo:")).toBeNull();
  });

  it("summarizes data quality and exposes detailed accessible profile semantics", () => {
    render(<SchemaReview dataset={reviewDatasetFixture} busy={false} draft={null} onDraftChange={vi.fn()} onConfirm={vi.fn()} />);

    expect(screen.getByLabelText("Dataset profile summary")).toHaveTextContent("Missing cells");
    expect(screen.getByRole("table")).toHaveAccessibleName(
      "Inferred types, completeness, uniqueness, and sample values",
    );
    expect(screen.getByRole("columnheader", { name: "Stored type" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "Inferred meaning" })).toBeVisible();
  });

  it("associates duplicate source mappings with the affected controls", () => {
    const duplicateDraft = {
      amount: "amount",
      date: "",
      customer_id: "customer",
      merchant: "merchant",
      location: "merchant",
      label: "",
    };

    render(
      <SchemaReview dataset={reviewDatasetFixture} busy={false} draft={duplicateDraft} onDraftChange={vi.fn()} onConfirm={vi.fn()} />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Each source column");
    expect(screen.getByLabelText("merchant")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("location")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Confirm schema" })).toBeDisabled();
  });

  it("filters a large profile without removing the table structure", async () => {
    render(<SchemaReview dataset={reviewDatasetFixture} busy={false} draft={null} onDraftChange={vi.fn()} onConfirm={vi.fn()} />);

    await userEvent.type(screen.getByLabelText("Filter columns"), "amount");

    expect(screen.getByRole("rowheader", { name: "amount" })).toBeVisible();
    expect(screen.queryByRole("rowheader", { name: "merchant" })).toBeNull();
    expect(screen.getByRole("table")).toBeVisible();
  });
});
