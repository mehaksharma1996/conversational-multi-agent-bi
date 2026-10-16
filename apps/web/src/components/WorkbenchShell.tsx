import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { SECTION_COPY, type WorkbenchSection } from "./workbenchNavigation";

interface NavigationItem {
  id: WorkbenchSection;
  label: string;
  description: string;
  icon: ReactNode;
}

const iconProps = {
  width: 18,
  height: 18,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

const NAVIGATION: NavigationItem[] = [
  {
    id: "overview",
    label: "Overview",
    description: "Workspace status and guided start",
    icon: <svg {...iconProps}><rect x="4" y="4" width="6" height="6" /><rect x="14" y="4" width="6" height="6" /><rect x="4" y="14" width="6" height="6" /><rect x="14" y="14" width="6" height="6" /></svg>,
  },
  {
    id: "data",
    label: "Data",
    description: "Upload and confirm a dataset",
    icon: <svg {...iconProps}><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></svg>,
  },
  {
    id: "analysis",
    label: "Analysis",
    description: "Deterministic findings and anomalies",
    icon: <svg {...iconProps}><path d="M4 19V9" /><path d="M10 19V5" /><path d="M16 19v-7" /><path d="M22 19H2" /></svg>,
  },
  {
    id: "documents",
    label: "Documents",
    description: "Index PDF guidance",
    icon: <svg {...iconProps}><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v5h5" /><path d="M9 13h6M9 17h6" /></svg>,
  },
  {
    id: "ask",
    label: "Ask",
    description: "Query data and documents",
    icon: <svg {...iconProps}><path d="M4 5h16v12H9l-5 4z" /><path d="M8 9h8M8 13h5" /></svg>,
  },
];

/** Must match the phone breakpoint in styles.css, where the sidebar becomes a drawer. */
const PHONE_QUERY = "(max-width: 760px)";

function subscribeToPhoneLayout(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const query = window.matchMedia(PHONE_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function isPhoneLayout(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(PHONE_QUERY).matches;
}

const FOCUSABLE = "a[href], button:not([disabled])";

interface WorkbenchShellProps {
  activeSection: WorkbenchSection;
  onNavigate: (section: WorkbenchSection) => void;
  workspaceStatus: string;
  workspaceReady: boolean;
  datasetLabel: string;
  documentLabel: string;
  analysisLabel: string;
  messageLabel: string;
  actionsDisabled: boolean;
  exportBusy: boolean;
  resetBusy: boolean;
  onExport: () => void;
  onReset: () => void;
  retentionNotice: ReactNode;
  children: ReactNode;
}

export function WorkbenchShell({
  activeSection,
  onNavigate,
  workspaceStatus,
  workspaceReady,
  datasetLabel,
  documentLabel,
  analysisLabel,
  messageLabel,
  actionsDisabled,
  exportBusy,
  resetBusy,
  onExport,
  onReset,
  retentionNotice,
  children,
}: WorkbenchShellProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const phone = useSyncExternalStore(subscribeToPhoneLayout, isPhoneLayout, () => false);
  const [lastPhone, setLastPhone] = useState(phone);
  const sidebarRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const restoreToggleFocus = useRef(false);
  const copy = SECTION_COPY[activeSection];

  // Leaving the phone layout always dismisses the drawer, so nothing stays inert on desktop.
  if (phone !== lastPhone) {
    setLastPhone(phone);
    setMobileOpen(false);
  }
  // At phone widths the open drawer is modal: the rest of the workbench is inert. The closed
  // drawer is inert too, so its off-screen links are neither focusable nor announced.
  const drawerOpen = phone && mobileOpen;
  const sidebarHidden = phone && !mobileOpen;

  // Entering the phone layout hides the sidebar; move focus out before the browser drops it.
  useLayoutEffect(() => {
    if (sidebarHidden && sidebarRef.current?.contains(document.activeElement)) {
      toggleRef.current?.focus();
    }
  }, [sidebarHidden]);

  useEffect(() => {
    if (drawerOpen) {
      const sidebar = sidebarRef.current;
      (
        sidebar?.querySelector<HTMLElement>('[aria-current="page"]') ??
        sidebar?.querySelector<HTMLElement>(FOCUSABLE)
      )?.focus();
      return;
    }
    if (restoreToggleFocus.current) {
      restoreToggleFocus.current = false;
      toggleRef.current?.focus();
    }
  }, [drawerOpen]);

  const closeDrawer = () => {
    restoreToggleFocus.current = true;
    setMobileOpen(false);
  };

  useEffect(() => {
    if (!drawerOpen) return;
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        restoreToggleFocus.current = true;
        setMobileOpen(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [drawerOpen]);

  // Keep Tab inside the modal drawer; the skip link sits outside the inert workbench frame.
  const containFocus = (event: KeyboardEvent<HTMLElement>) => {
    if (!drawerOpen || event.key !== "Tab") return;
    const focusable = Array.from(
      sidebarRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [],
    ).filter((element) => element.getClientRects().length > 0);
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const navigate = (section: WorkbenchSection) => {
    setMobileOpen(false);
    onNavigate(section);
  };

  return (
    <div className={`workbench-shell ${collapsed ? "workbench-shell--collapsed" : ""}`}>
      <div
        id="workbench-navigation"
        ref={sidebarRef}
        className={`sidebar ${drawerOpen ? "sidebar--mobile-open" : ""}`}
        aria-label="Workbench navigation"
        role={drawerOpen ? "dialog" : "complementary"}
        aria-modal={drawerOpen ? true : undefined}
        inert={sidebarHidden}
        onKeyDown={containFocus}
      >
        <div className="sidebar-brand">
          <span className="brand-mark" aria-hidden="true">CB</span>
          <span className="brand-copy">
            <strong>Conversational BI</strong>
            <small>Analyst workbench</small>
          </span>
          <button
            className="sidebar-close"
            type="button"
            aria-label="Close navigation"
            onClick={closeDrawer}
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>

        <nav className="primary-nav" aria-label="Primary">
          {NAVIGATION.map((item) => (
            <a
              key={item.id}
              className={`nav-item ${activeSection === item.id ? "nav-item--active" : ""}`}
              href={`./#${item.id}`}
              aria-current={activeSection === item.id ? "page" : undefined}
              aria-label={collapsed ? item.label : undefined}
              title={collapsed ? item.label : undefined}
              onClick={(event) => {
                event.preventDefault();
                navigate(item.id);
              }}
            >
              <span className="nav-item__icon">{item.icon}</span>
              <span className="nav-item__copy">
                <strong>{item.label}</strong>
                <small>{item.description}</small>
              </span>
            </a>
          ))}
        </nav>

        <div className="sidebar-status">
          <span className={`status-dot ${workspaceReady ? "status-dot--ready" : ""}`} aria-hidden="true" />
          <span className="sidebar-status__copy">
            <strong>{workspaceReady ? "Workspace ready" : "Workspace unavailable"}</strong>
            <small>Local, isolated session</small>
          </span>
        </div>

        <button
          className="sidebar-collapse"
          type="button"
          aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
          aria-expanded={!collapsed}
          onClick={() => setCollapsed((current) => !current)}
        >
          <span aria-hidden="true">{collapsed ? "›" : "‹"}</span>
          <span className="sidebar-collapse__label">Collapse</span>
        </button>
      </div>

      {/* Pointer-only dismissal; keyboard users have Escape and the labelled close button. */}
      {drawerOpen ? <div className="sidebar-backdrop" aria-hidden="true" onClick={closeDrawer} /> : null}

      <div className="workbench-frame" inert={drawerOpen}>
        <header className="context-bar">
          <button
            ref={toggleRef}
            className="mobile-nav-toggle"
            type="button"
            aria-label={drawerOpen ? "Close navigation" : "Open navigation"}
            aria-controls="workbench-navigation"
            aria-expanded={drawerOpen}
            onClick={() => (drawerOpen ? closeDrawer() : setMobileOpen(true))}
          >
            <span aria-hidden="true">☰</span>
          </button>

          <div className="workspace-identity" role="status">
            <span className={`status-dot ${workspaceReady ? "status-dot--ready" : ""}`} aria-hidden="true" />
            <span>
              <strong>{workspaceStatus}</strong>
              <small>Private workspace</small>
            </span>
          </div>

          <dl className="context-summary" aria-label="Active workspace context">
            <div><dt>Data</dt><dd>{datasetLabel}</dd></div>
            <div><dt>Documents</dt><dd>{documentLabel}</dd></div>
            <div><dt>Analysis</dt><dd>{analysisLabel}</dd></div>
            <div><dt>Conversation</dt><dd>{messageLabel}</dd></div>
          </dl>

          <div className="context-actions">
            {/* The short label is for phone widths; the accessible name stays the full action. */}
            <button
              className="button button--secondary button--compact"
              type="button"
              aria-label={exportBusy ? "Preparing workspace export" : "Export workspace"}
              disabled={actionsDisabled}
              onClick={onExport}
            >
              {exportBusy ? (
                "Preparing…"
              ) : (
                <>
                  <span className="action-label--full">Export workspace</span>
                  <span className="action-label--short">Export</span>
                </>
              )}
            </button>
            <button className="button button--danger-quiet button--compact" type="button" aria-label="Reset workspace" disabled={actionsDisabled} onClick={onReset}>
              {resetBusy ? "Resetting…" : "Reset"}
            </button>
          </div>
        </header>

        <main id="main-content" className="workbench-main" tabIndex={-1}>
          <header className="page-heading" data-section-heading tabIndex={-1}>
            <p className="eyebrow">{copy.eyebrow}</p>
            <h1>{copy.title}</h1>
            <p>{copy.description}</p>
          </header>
          {children}
        </main>

        <footer className="workbench-footer">{retentionNotice}</footer>
      </div>
    </div>
  );
}
