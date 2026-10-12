/**
 * The synthetic retail-operations demo.
 *
 * The files are served from /demo and loaded through the ordinary upload endpoints, so the demo
 * gets the same validation, schema review, and tenant isolation as any other upload. Nothing is
 * preloaded server-side.
 *
 * Every sample question below has a committed, machine-readable answer in
 * sample_data/retail_operations_ground_truth.json (`groundTruthId`); a unit test keeps the two in
 * step. Which route actually answers a question is up to the configured model, so the route shown
 * here is the one the question is designed to exercise.
 */
export const DEMO_DATA_FILENAME = "retail_operations.csv";
export const DEMO_POLICY_FILENAME = "retail_operations_policy.pdf";

interface DemoFile {
  url: string;
  filename: string;
  type: string;
}

export const DEMO_DATA_FILE: DemoFile = {
  url: `/demo/${DEMO_DATA_FILENAME}`,
  filename: DEMO_DATA_FILENAME,
  type: "text/csv",
};

export const DEMO_POLICY_FILE: DemoFile = {
  url: `/demo/${DEMO_POLICY_FILENAME}`,
  filename: DEMO_POLICY_FILENAME,
  type: "application/pdf",
};

export async function fetchDemoFile(spec: DemoFile): Promise<File> {
  const response = await fetch(spec.url, { cache: "no-store" });
  const contentType = response.headers.get("Content-Type") ?? "";
  // The static server falls back to index.html for unknown paths; that is not a demo file.
  if (!response.ok || contentType.includes("text/html")) {
    throw new Error(`The demo file ${spec.filename} could not be loaded.`);
  }
  return new File([await response.blob()], spec.filename, { type: spec.type });
}

/**
 * Canonical fields the demo deliberately leaves unmapped, with the reason shown in schema review.
 * The mapper's suggestion for `label` is `status`, but in this table `status` records whether a
 * documented review rule matched. It is derived from other columns, so it is not an outcome to
 * predict, and mapping it would offer a classifier that only rediscovers the rule. This is advice
 * for the one bundled file, identified by name; the mapper's behaviour for other data is unchanged.
 */
export const DEMO_UNMAPPED_FIELDS: Readonly<Record<string, string>> = {
  label:
    "status marks which transactions matched a documented review rule. It is derived from other " +
    "columns, so it is not an outcome to predict.",
};

export type DemoNeeds = "data" | "policy" | "both";

export interface DemoQuestion {
  /** Matches an id in sample_data/retail_operations_ground_truth.json. */
  groundTruthId: string;
  category: string;
  question: string;
  route: "sql" | "rag" | "hybrid";
  needs: DemoNeeds;
  /** Text a correct answer or its result table must contain, taken from the ground truth. */
  expected: string[];
}

export const DEMO_QUESTIONS: readonly DemoQuestion[] = [
  {
    groundTruthId: "total-revenue",
    category: "Headline metric",
    question: "What is total revenue across the dataset?",
    route: "sql",
    needs: "data",
    expected: ["1625730.59"],
  },
  {
    groundTruthId: "top-region-revenue",
    category: "Regional performance",
    question: "Which region has the highest total revenue?",
    route: "sql",
    needs: "data",
    expected: ["West", "533933.01"],
  },
  {
    groundTruthId: "top-category-profit",
    category: "Category and margin",
    question: "Which category generated the most gross profit?",
    route: "sql",
    needs: "data",
    expected: ["Electronics", "196361.54"],
  },
  {
    groundTruthId: "peak-month-revenue",
    category: "Time trend",
    question: "Which month had the highest revenue?",
    route: "sql",
    needs: "data",
    expected: ["2025-10", "113432.96"],
  },
  {
    groundTruthId: "h1-year-comparison",
    category: "Period comparison",
    question: "How did first-half revenue change from 2025 to 2026?",
    route: "sql",
    needs: "data",
    expected: ["503347.58", "529303.93"],
  },
  {
    groundTruthId: "missing-segments",
    category: "Data quality",
    question: "How many transactions have an unknown customer segment?",
    route: "sql",
    needs: "data",
    expected: ["24"],
  },
  {
    groundTruthId: "policy-return-rule",
    category: "Policy document",
    question: "Does a return by itself require escalation?",
    route: "rag",
    needs: "policy",
    expected: ["does not by itself require escalation"],
  },
  {
    groundTruthId: "fulfillment-escalation-count",
    category: "Fulfillment exceptions",
    question: "How many transactions meet the policy fulfillment escalation rule?",
    route: "hybrid",
    needs: "both",
    expected: ["13"],
  },
  {
    groundTruthId: "policy-review-count",
    category: "Discount and margin policy",
    question: "How many transactions meet at least one documented review rule?",
    route: "hybrid",
    needs: "both",
    expected: ["45"],
  },
];
