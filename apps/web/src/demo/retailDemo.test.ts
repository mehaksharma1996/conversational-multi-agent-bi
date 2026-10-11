/// <reference types="node" />
import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { DEMO_QUESTIONS } from "./retailDemo";

// Vitest runs from apps/web, so the committed sample data is two directories up.
const webRoot = process.cwd();
const sampleData = path.resolve(webRoot, "../../sample_data");
const groundTruthJson = readFileSync(
  path.join(sampleData, "retail_operations_ground_truth.json"),
  "utf-8",
);

interface GroundTruthQuestion {
  id: string;
  question: string;
  answer: Record<string, string | number | boolean>;
}

const groundTruth = JSON.parse(groundTruthJson) as { questions: GroundTruthQuestion[] };

describe("retail demo sample questions", () => {
  it("each question is a committed ground-truth question with its exact wording", () => {
    for (const item of DEMO_QUESTIONS) {
      const truth = groundTruth.questions.find((entry) => entry.id === item.groundTruthId);
      expect(truth, item.groundTruthId).toBeDefined();
      expect(item.question).toBe(truth?.question);
    }
  });

  it("each expected value appears in the committed answer, so nothing is invented in the UI", () => {
    for (const item of DEMO_QUESTIONS) {
      const truth = groundTruth.questions.find((entry) => entry.id === item.groundTruthId);
      const values = Object.values(truth?.answer ?? {}).map(String);
      for (const expected of item.expected) {
        const found = values.some((value) => value.includes(expected));
        expect(found, `${item.groundTruthId}: ${expected}`).toBe(true);
      }
    }
  });

  it("covers every committed ground-truth question exactly once", () => {
    const ids = DEMO_QUESTIONS.map((item) => item.groundTruthId).sort();
    expect(ids).toEqual(groundTruth.questions.map((entry) => entry.id).sort());
  });

  it("the served CSV matches the committed sample data (the PDF is checked in pytest)", () => {
    const served = readFileSync(path.join(webRoot, "public/demo/retail_operations.csv"));
    const source = readFileSync(path.join(sampleData, "retail_operations.csv"));
    expect(served.equals(source)).toBe(true);
  });
});
