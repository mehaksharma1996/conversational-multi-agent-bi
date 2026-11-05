import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TabularCapabilities } from "../api/client";
import { UploadStep } from "./UploadStep";

const capabilities: TabularCapabilities = {
  supported_extensions: [".csv", ".xlsx"],
  max_upload_bytes: 10,
  max_rows: 500,
};

function renderStep(overrides: Partial<React.ComponentProps<typeof UploadStep>> = {}) {
  const props: React.ComponentProps<typeof UploadStep> = {
    disabled: false,
    capabilities,
    capabilitiesUnavailable: false,
    operation: null,
    sheets: [],
    file: null,
    sheet: "",
    onFileChange: vi.fn(),
    onSheetChange: vi.fn(),
    onUpload: vi.fn(),
    onSelectSheet: vi.fn(),
    onCancel: vi.fn(),
    onRetryCapabilities: vi.fn(),
    ...overrides,
  };
  render(<UploadStep {...props} />);
  return props;
}

describe("UploadStep", () => {
  it("shows active server limits and rejects unsupported files before submission", async () => {
    const props = renderStep();
    const user = userEvent.setup({ applyAccept: false });

    expect(screen.getByText("Up to 500 rows")).toBeVisible();
    await user.upload(
      screen.getByLabelText("CSV or Excel file"),
      new File(["data"], "notes.txt", { type: "text/plain" }),
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Choose .csv, .xlsx file.");
    expect(props.onFileChange).toHaveBeenLastCalledWith(null);
    expect(screen.getByRole("button", { name: "Upload and profile" })).toBeDisabled();
  });

  it("rejects a file larger than the advertised limit", async () => {
    renderStep();

    await userEvent.upload(
      screen.getByLabelText("CSV or Excel file"),
      new File(["01234567890"], "large.csv", { type: "text/csv" }),
    );

    expect(screen.getByRole("alert")).toHaveTextContent("larger than the 1 KB server limit");
  });

  it("shows truthful progress and offers stop-waiting only for cancellable requests", async () => {
    const props = renderStep({
      operation: {
        phase: "running",
        progress: { stage: "Profiling columns", completed: 2, total: 3 },
        cancellable: true,
      },
    });

    expect(screen.getByText("Profiling columns")).toBeVisible();
    expect(screen.getByRole("progressbar")).toHaveAttribute("value", "2");
    await userEvent.click(screen.getByRole("button", { name: "Stop waiting and check" }));
    expect(props.onCancel).toHaveBeenCalledOnce();
    expect(screen.getByText("cannot promise that server work stopped", { exact: false })).toBeVisible();
  });
});
