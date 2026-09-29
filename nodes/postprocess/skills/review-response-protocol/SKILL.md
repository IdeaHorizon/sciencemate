---
name: review-response-protocol
description: Convert mechanical validation and VLM-visible observations into deterministic revision actions without granting the VLM authority over scientific truth.
---

# Review Response Protocol

## Review sequence

1. Run deterministic checks first: files, hashes, dimensions, formats, source bindings, plan binding, and panel lineage.
2. In `standard`, visually review the composed asset. In `publication`, review each panel; add a whole-figure composition review only for a true multi-panel or composite asset.
3. Ask the VLM only for a visible region and visible phenomenon. Do not ask it to decide scientific correctness, severity, or approval.
4. Map observations to rubric, severity, and action with deterministic policy.
5. Re-render after actionable defects. Require the review PNG content hash to change, then repeat both validation and review against the new plan, figure, and image hashes.

## Fail-closed behavior

Unavailable, malformed, timed-out, or stale required review means the quality gate is not satisfied. A revised plan that produces a byte-identical review PNG is an ineffective revision: do not call the VLM again and do not upgrade the package. `quick` mode may intentionally omit VLM review, but it must still pass mechanical validation.
