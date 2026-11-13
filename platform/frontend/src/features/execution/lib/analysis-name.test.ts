import assert from "node:assert/strict";
import test from "node:test";
import { nodeDisplayName } from "./display-names.ts";
import { artifactKindLabel } from "../../catalog/lib/kind-label.ts";
import { parseRunLineage } from "./research-map.ts";

test("both node names display the canonical Analysis identity", () => {
  for (const node of ["analysis", "hypothesis"]) {
    assert.equal(nodeDisplayName(node, "en"), "Analysis");
    assert.equal(nodeDisplayName(node, "zh"), "分析");
  }
  assert.equal(nodeDisplayName("other_node", "en"), "other_node");
});

test("old lineage parses to current node types without changing stored run IDs", () => {
  const runId = "run_root::hypothesis->hypothesis@d2";
  assert.deepEqual(parseRunLineage(runId), { dispatcher: "analysis", nodeType: "analysis" });
  assert.equal(runId, "run_root::hypothesis->hypothesis@d2");
});

test("old and current node-wide artifact kinds have the same readable labels", () => {
  for (const lang of ["zh", "en"] as const) {
    assert.equal(artifactKindLabel("hypothesis_research_overview", lang),
      artifactKindLabel("analysis_research_overview", lang));
    assert.equal(artifactKindLabel("hypothesis_output_validation", lang),
      artifactKindLabel("analysis_output_validation", lang));
  }
});
