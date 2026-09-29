---
name: scientific-visual-integrity
description: Enforce truthful scientific encodings, uncertainty semantics, axis and scale integrity, and strict separation between upstream analysis and visualization.
---

# Scientific Visual Integrity

## Non-negotiable rules

- Bind every mark to an upstream field or a lineage-recorded display derivation.
- Preserve units, category order, sample identity, missingness, and sign unless the plan explicitly states a truthful transform.
- Never invent uncertainty. Label whether an interval is SD, SE, CI, range, or another upstream-defined quantity.
- Do not smooth, filter, aggregate, normalize scientifically, remove outliers, or run significance tests in this node.
- Use truncated axes, logarithmic scales, dual axes, area/volume encodings, and nonlinear color normalization only when explicitly declared and scientifically justified.
- Do not connect observations across missing temporal or spatial intervals in a way that implies measured continuity.
- Keep legends, annotations, and decorative layers from hiding data.

## Escalation

If an upstream artifact lacks the semantics required for a truthful visual, stop and request upstream rework. Do not silently repair scientific ambiguity in rendering code.
