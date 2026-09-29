---
name: visual-provenance
description: Track source bindings, display-only derivations, plan and output hashes, panel lineage, and review freshness for reproducible scientific figures.
---

# Visual Provenance

## Required lineage

Record for every figure or panel:

- source artifact IDs, file paths, and content hashes;
- bound fields or image channels;
- display-only derivations and their parameters;
- the visual plan hash and renderer/backend identity;
- exact output file hashes, dimensions, and formats;
- the review target hash and reviewer configuration.

## Rules

Raw sources are immutable. Derived display data must remain distinguishable from scientific results. A review is current only when its target hash exactly matches the rendered asset. Composition must retain the lineage of each panel. Final packaging must fail closed on a missing or stale required review.
