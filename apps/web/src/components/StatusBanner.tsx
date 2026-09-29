import type { ReactNode } from "react";

interface StatusBannerProps {
  kind: "info" | "error" | "success";
  children: ReactNode;
  requestId?: string | null;
}

export function StatusBanner({ kind, children, requestId }: StatusBannerProps) {
  const role = kind === "error" ? "alert" : "status";
  return (
    <div className={`status-banner status-banner--${kind}`} role={role}>
      <div>{children}</div>
      {requestId ? <small>Request ID: {requestId}</small> : null}
    </div>
  );
}
