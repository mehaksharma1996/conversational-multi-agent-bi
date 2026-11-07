import type { ReactNode } from "react";

interface StatusBannerProps {
  kind: "info" | "error" | "success";
  children: ReactNode;
  requestId?: string | null;
  focusTarget?: boolean;
}

export function StatusBanner({ kind, children, requestId, focusTarget = false }: StatusBannerProps) {
  const role = kind === "error" ? "alert" : "status";
  return (
    <div
      className={`status-banner status-banner--${kind}`}
      role={role}
      tabIndex={kind === "error" ? -1 : undefined}
      data-error-focus={focusTarget || undefined}
    >
      <div>{children}</div>
      {requestId ? (
        <small>
          Request ID: {requestId}
          {kind === "error" ? " (quote this when asking for help)" : ""}
        </small>
      ) : null}
    </div>
  );
}
