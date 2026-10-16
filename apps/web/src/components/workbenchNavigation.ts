export type WorkbenchSection = "overview" | "data" | "analysis" | "documents" | "ask";

const SECTIONS = new Set<string>(["overview", "data", "analysis", "documents", "ask"]);

/**
 * Maps a URL fragment to the workbench section it routes to. An empty fragment is the overview,
 * and a citation fragment (`#source-…`, from older links or history entries) belongs to Ask. Any
 * other fragment, such as the skip link's `#main-content`, is not a route and returns null.
 */
export function sectionForHash(hash: string): WorkbenchSection | null {
  const fragment = hash.startsWith("#") ? hash.slice(1) : hash;
  if (fragment === "") return "overview";
  if (SECTIONS.has(fragment)) return fragment as WorkbenchSection;
  if (fragment.startsWith("source-")) return "ask";
  return null;
}

export const SECTION_COPY: Record<
  WorkbenchSection,
  { eyebrow: string; title: string; description: string }
> = {
  overview: {
    eyebrow: "Workspace overview",
    title: "Ask your data direct questions, with the evidence in view.",
    description: "Review the active workspace, continue setup, or open an analysis workflow.",
  },
  data: {
    eyebrow: "Data workspace",
    title: "Prepare a trusted dataset",
    description: "Upload a table, inspect its profile, and confirm the canonical schema before analysis.",
  },
  analysis: {
    eyebrow: "Analysis workspace",
    title: "Review deterministic findings",
    description: "Configure anomaly review and inspect summaries, charts, candidates, and reports.",
  },
  documents: {
    eyebrow: "Document workspace",
    title: "Ground answers in source documents",
    description: "Index text-based PDF guidance for page-aware retrieval and citations.",
  },
  ask: {
    eyebrow: "Conversation workspace",
    title: "Ask across data and documents",
    description: "Inspect the route, evidence, SQL, sources, and result rows behind every answer.",
  },
};
