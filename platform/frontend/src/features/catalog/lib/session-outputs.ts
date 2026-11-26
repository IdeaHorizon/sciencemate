import type { CatalogEntry } from "../../../lib/api.ts";

const PAPER_KINDS = new Set(["manuscript", "accepted_paper", "paper_pdf", "paper_draft"]);
const REPORT_KINDS = new Set(["pre_registration", "search_protocol", "research_plan", "analysis_report", "survey_report", "review_report", "derivation_log"]);
const FIGURE_KINDS = new Set(["figure", "figure_package"]);
const DATA_KINDS = new Set(["clean_results", "raw_results", "observation_results", "dataset_snapshot"]);

/** Keep catalog retention decisions; group useful outputs without exposing internal working files. */
export function sessionOutputs(entries: readonly CatalogEntry[], query = "") {
  const needle = query.trim().toLocaleLowerCase();
  const visible = entries.filter((entry) => entry.tier !== "working" && (!needle ||
    [entry.name, entry.kind, entry.recordPath, ...entry.files].join(" ").toLocaleLowerCase().includes(needle)));
  const groups: Record<"primary" | "figures" | "data" | "other", CatalogEntry[]> = {
    primary: [], figures: [], data: [], other: [],
  };
  for (const entry of visible) {
    const group = FIGURE_KINDS.has(entry.kind) ? "figures" : DATA_KINDS.has(entry.kind) ? "data"
      : PAPER_KINDS.has(entry.kind) || REPORT_KINDS.has(entry.kind) || entry.isDeliverable ? "primary" : "other";
    groups[group].push(entry);
  }
  // Manuscripts stay reachable even before finalization; batches of data must not bury them.
  groups.primary.sort((a, b) => Number(PAPER_KINDS.has(b.kind)) - Number(PAPER_KINDS.has(a.kind)));
  return groups;
}

/** Preserve the catalog's preferred readable file, retaining access to every companion and the record. */
export function outputPaths(entry: CatalogEntry): string[] {
  return [...new Set([...entry.files, entry.recordPath].filter((path) => path.trim()))];
}
