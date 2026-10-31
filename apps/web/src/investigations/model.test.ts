import { describe, expect, it } from "vitest";

import type { Investigation, InvestigationCostEstimate } from "../api/client";
import {
  describePlanningCost,
  isTerminalInvestigation,
  needsPlanDecision,
  shouldPollInvestigation,
} from "./model";

function estimate(overrides: Partial<InvestigationCostEstimate>): InvestigationCostEstimate {
  return {
    kind: "estimate",
    basis: "token_limits_x_highest_eligible_price",
    bound: "unavailable",
    amount_usd: null,
    limitations: ["pricing_unknown"],
    ...overrides,
  };
}

describe("investigation model", () => {
  it("classifies only active server-work states for polling", () => {
    expect(shouldPollInvestigation("planning")).toBe(true);
    expect(shouldPollInvestigation("running")).toBe(true);
    expect(shouldPollInvestigation("awaiting_plan_approval")).toBe(false);
    expect(isTerminalInvestigation("interrupted")).toBe(true);
    expect(isTerminalInvestigation("awaiting_sql_approval")).toBe(false);
  });

  it("requires every server binding before enabling a plan decision", () => {
    const investigation = {
      id: "inv_one",
      conversation_id: "conv_one",
      message_id: "msg_one",
      status: "awaiting_plan_approval",
      plan: { objective: "Review churn" },
      proposal: { cost_estimate: estimate({}), missing_information: [], approval_requirements: [], plan_expires_at: "2026-10-31T00:00:00Z" },
      plan_digest: "a".repeat(64),
      authority_epoch: 1,
      created_at: "2026-10-30T00:00:00Z",
      updated_at: "2026-10-30T00:00:00Z",
    } as Investigation;
    expect(needsPlanDecision(investigation)).toBe(true);
    expect(needsPlanDecision({ ...investigation, proposal: null })).toBe(false);
    expect(needsPlanDecision({ ...investigation, authority_epoch: null })).toBe(false);
  });

  it("calls only an enforced estimate an upper bound", () => {
    expect(
      describePlanningCost(
        estimate({ bound: "enforced_upper_bound", amount_usd: 0.1234, limitations: [] }),
      ),
    ).toBe("Estimated planning cost: up to $0.1234 under the current enforced provider limits.");
    expect(
      describePlanningCost(
        estimate({ bound: "indicative", amount_usd: 0.5, limitations: ["retries_not_metered"] }),
      ),
    ).toContain("This is not a maximum because provider retries are not fully metered.");
    expect(describePlanningCost(estimate({}))).toBe(
      "A planning-cost estimate is unavailable because pricing is unknown for at least one eligible provider.",
    );
  });
});
