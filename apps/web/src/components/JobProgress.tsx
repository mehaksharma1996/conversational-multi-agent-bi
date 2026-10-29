import { describeJob, type JobView } from "../operations/useJob";

interface JobProgressProps {
  /** A plain description such as "the investigation"; never an identifier. */
  label: string;
  view: JobView;
  onCancel?: () => void;
  onRefresh?: () => void;
}

/**
 * Status, determinate progress (only when the server reports a total), and the honest actions for a
 * background job: Cancel while it can be requested, Check again after polling stopped unanswered.
 * Announcements go through the workbench's single live region, so this region is not live itself.
 */
export function JobProgress({ label, view, onCancel, onRefresh }: JobProgressProps) {
  if (view.state === "idle") return null;
  const active = view.state === "active";
  const total = active && view.job?.progress_total ? view.job.progress_total : null;
  return (
    <div className="job-progress" aria-busy={active || undefined}>
      <p>{describeJob(view, label)}</p>
      {total !== null && view.state === "active" && view.job ? (
        <progress
          max={total}
          value={Math.min(view.job.progress_completed, total)}
          aria-label={`Progress of ${label}`}
        />
      ) : null}
      {active && onCancel && (view.cancel === "none" || view.cancel === "failed") ? (
        <button className="button button--secondary" type="button" onClick={onCancel}>
          Cancel {label}
        </button>
      ) : null}
      {view.state === "unknown" && view.reason !== "not_found" && onRefresh ? (
        <button className="button button--link" type="button" onClick={onRefresh}>
          Check again
        </button>
      ) : null}
    </div>
  );
}
