import test from "node:test";
import assert from "node:assert/strict";
import type { CatalogEntry } from "../../../lib/api.ts";
import { outputPaths, sessionOutputs } from "./session-outputs.ts";

const entry = (patch: Partial<CatalogEntry> = {}): CatalogEntry => ({
  artifactId: "paper", kind: "manuscript", name: "Research paper", ownerNode: "writing", version: 3,
  frozen: true, permanent: true, tier: "deliverable", isDeliverable: true,
  recordPath: "paper/manuscript.md", files: ["paper/manuscript.pdf"], createdAt: "", frozenAt: "", producedByRunId: "", ...patch,
});

test("papers stay visible while framework logs are excluded regardless of frozen flags", () => {
  const paper = entry();
  const draft = entry({ artifactId: "draft", tier: "output", isDeliverable: false, frozen: false });
  const internal = entry({ artifactId: "internal", tier: "working", kind: "latex_build_receipt" });
  assert.deepEqual(sessionOutputs([internal, paper, draft]), { primary: [paper, draft], figures: [], data: [], other: [] });
});

test("search reaches companion files without reintroducing process files", () => {
  const paper = entry();
  const internal = entry({ artifactId: "internal", tier: "working" });
  assert.deepEqual(sessionOutputs([paper, internal], " MANUSCRIPT.PDF ").primary, [paper]);
  assert.deepEqual(sessionOutputs([paper], "not present"), { primary: [], figures: [], data: [], other: [] });
});

test("batches of frozen data do not bury an unfinished manuscript, and new finalized kinds remain visible", () => {
  const data = entry({ kind: "clean_results" });
  const plan = entry({ kind: "pre_registration" });
  const paper = entry({ frozen: false, isDeliverable: false, tier: "output" });
  const unknown = entry({ kind: "new_finalized_kind" });
  const grouped = sessionOutputs([data, plan, unknown, paper]);
  assert.deepEqual(grouped.primary, [paper, plan, unknown]);
  assert.deepEqual(grouped.data, [data]);
});

test("prefer the catalog's readable file and keep fallback records and companions accessible", () => {
  assert.deepEqual(outputPaths(entry({ files: ["paper/main.pdf", "paper/main.pdf", "paper/source.zip"] })),
    ["paper/main.pdf", "paper/source.zip", "paper/manuscript.md"]);
  assert.deepEqual(outputPaths(entry({ files: [] })), ["paper/manuscript.md"]);
  assert.deepEqual(outputPaths(entry({ files: [], recordPath: "" })), []);
});
