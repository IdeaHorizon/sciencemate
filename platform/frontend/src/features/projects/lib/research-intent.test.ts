import test from "node:test";
import assert from "node:assert/strict";
import { researchIntentFingerprint } from "./research-intent.ts";

test("research intent dirty tracking ignores object insertion order", () => {
  const original = {
    domain: "Materials",
    convergence_policy: { min_reviews: 2, accept_min_overall: 3.8 },
  };
  const reordered = {
    convergence_policy: { accept_min_overall: 3.8, min_reviews: 2 },
    domain: "Materials",
  };

  assert.equal(researchIntentFingerprint(original), researchIntentFingerprint(reordered));
});

test("research intent dirty tracking detects a policy change", () => {
  const original = { convergence_policy: { min_reviews: 2 } };
  const changed = { convergence_policy: { min_reviews: 3 } };
  assert.notEqual(researchIntentFingerprint(original), researchIntentFingerprint(changed));
});
