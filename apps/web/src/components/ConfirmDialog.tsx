import { useEffect, useId, useRef, type ReactNode } from "react";

export interface ConfirmationRequest {
  title: string;
  /** Each consequence the person is agreeing to, stated plainly. */
  consequences: ReactNode[];
  confirmLabel: string;
  tone?: "danger" | "default";
}

interface ConfirmDialogProps {
  request: ConfirmationRequest | null;
  onResolve: (confirmed: boolean) => void;
}

/**
 * A modal confirmation built on the native `<dialog>` element: `showModal()` makes the rest of the
 * page inert (so focus stays inside), Escape cancels, and focus returns to the control that opened
 * it, or to the section heading if that control is gone. Cancel is focused first, so a stray Enter
 * never confirms a destructive action.
 */
export function ConfirmDialog({ request, onResolve }: ConfirmDialogProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const bodyId = useId();
  const open = request !== null;

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (open && !element.open) {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      element.showModal();
      cancel.current?.focus();
    } else if (!open && element.open) {
      element.close();
    }
  }, [open]);

  const resolve = (confirmed: boolean) => {
    onResolve(confirmed);
    const target = opener.current;
    opener.current = null;
    window.requestAnimationFrame(() => {
      if (target?.isConnected && !target.matches(":disabled")) target.focus();
      else document.querySelector<HTMLElement>("[data-section-heading]")?.focus();
    });
  };

  return (
    <dialog
      ref={dialog}
      className="confirm-dialog"
      aria-labelledby={titleId}
      aria-describedby={bodyId}
      onCancel={(event) => {
        event.preventDefault();
        resolve(false);
      }}
    >
      {request ? (
        <form method="dialog" onSubmit={(event) => event.preventDefault()}>
          <h2 id={titleId}>{request.title}</h2>
          <ul id={bodyId} className="confirm-dialog__consequences">
            {request.consequences.map((consequence, index) => (
              <li key={index}>{consequence}</li>
            ))}
          </ul>
          <div className="button-row">
            <button ref={cancel} className="button button--secondary" type="button" onClick={() => resolve(false)}>
              Cancel
            </button>
            <button
              className={request.tone === "danger" ? "button button--danger" : "button button--primary"}
              type="button"
              onClick={() => resolve(true)}
            >
              {request.confirmLabel}
            </button>
          </div>
        </form>
      ) : null}
    </dialog>
  );
}
