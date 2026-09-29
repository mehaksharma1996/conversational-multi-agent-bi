import { render, screen } from "@testing-library/react";
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

    render(<SchemaReview dataset={dataset} busy={false} onConfirm={vi.fn()} />);

    expect(screen.getByLabelText("amount")).toHaveValue("");
    expect(screen.getByText("Low-confidence suggestions are not selected automatically", { exact: false })).toBeVisible();
  });
});
