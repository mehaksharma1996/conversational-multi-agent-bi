import type {
  Investigation,
  InvestigationCostEstimate,
  InvestigationStatus,
} from "../api/client";

const TERMINAL = new Set<InvestigationStatus>([
  "completed",
  "completed_partial",
  "budget_exhausted",
  "plan_invalid",
  "plan_rejected",
  "cancelled",
  "failed",
  "interrupted",
  "expired",
]);

const POLLED = new Set<InvestigationStatus>(["planning", "queued", "running"]);

export function isTerminalInvestigation(status: InvestigationStatus): boolean {
  return TERMINAL.has(status);
}

export function shouldPollInvestigation(status: InvestigationStatus): boolean {
  return POLLED.has(status);
}

export function needsPlanDecision(investigation: Investigation): boolean {
  return (
    investigation.status === "awaiting_plan_approval" &&
    investigation.plan !== null &&
    investigation.plan !== undefined &&
    investigation.proposal !== null &&
    investigation.proposal !== undefined &&
    typeof investigation.plan_digest === "string" &&
    typeof investigation.authority_epoch === "number"
  );
}

export function needsSqlWaveDecision(investigation: Investigation): boolean {
  return investigation.status === "awaiting_sql_approval";
}

type Limitation = InvestigationCostEstimate["limitations"][number];

const LIMITATION_TEXT: Record<Limitation, string> = {
  no_eligible_provider: "no eligible provider is configured",
  pricing_unknown: "pricing is unknown for at least one eligible provider",
  output_cap_not_enforced: "an eligible provider does not enforce the output cap",
  input_not_bounded: "input is not bounded before every eligible provider call",
  retries_not_metered: "provider retries are not fully metered",
};

function usd(amount: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(amount);
}

/** Honest wording for the proposal's planning-phase estimate; never execution cost. */
export function describePlanningCost(estimate: InvestigationCostEstimate): string {
  if (estimate.bound === "unavailable" || estimate.amount_usd === null || estimate.amount_usd === undefined) {
    const why = estimate.limitations.map((item) => LIMITATION_TEXT[item]).join("; ");
    return `A planning-cost estimate is unavailable${why ? ` because ${why}` : ""}.`;
  }
  const amount = usd(estimate.amount_usd);
  if (estimate.bound === "enforced_upper_bound") {
    return `Estimated planning cost: up to ${amount} under the current enforced provider limits.`;
  }
  const why = estimate.limitations.map((item) => LIMITATION_TEXT[item]).join("; ");
  return `Indicative planning-cost estimate: ${amount}. This is not a maximum${why ? ` because ${why}` : ""}.`;
}

export const INVESTIGATION_STATUS_LABEL: Record<InvestigationStatus, string> = {
  planning: "Planning",
  awaiting_plan_approval: "Awaiting plan approval",
  queued: "Queued",
  running: "Running",
  awaiting_sql_approval: "Awaiting SQL approval",
  completed: "Completed",
  completed_partial: "Completed with limitations",
  budget_exhausted: "Budget exhausted",
  plan_invalid: "Plan invalid",
  plan_rejected: "Plan rejected",
  cancelled: "Cancelled",
  failed: "Failed",
  interrupted: "Interrupted",
  expired: "Expired",
};

