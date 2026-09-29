---
name: clinical-diagnostic-and-survival
description: Design clinical effect, agreement, survival, classifier-diagnostic, and calibration figures from upstream-computed estimates and display-ready coordinates without performing clinical statistics in the visualization node.
---

# Clinical, Diagnostic, and Survival Figures

## Scientific boundary

Use only upstream estimates, intervals, event/censor indicators, diagnostic-curve coordinates,
limits of agreement, and calibration bins. Never compute Kaplan–Meier estimates, confidence
intervals, AUC, thresholds, sensitivity/specificity, calibration bins, agreement limits, pooled
effects, or p-values here. Keep experimental unit, endpoint definition, population, interval kind,
and null/reference value explicit.

## Visual grammar

- Effect estimates: forest/interval display with a declared measure, interval semantics, and null.
- Survival: precomputed non-increasing step coordinates in `[0, 1]`, upstream interval endpoints,
  and censor ticks only from an explicit censor field.
- ROC/PR/calibration: precomputed `[0, 1]` coordinates and a curve contract; never infer AUC.
- Agreement: precomputed pair mean/difference plus upstream bias and both limits of agreement.

Use redundant group encodings, restrained reference lines, and direct labels when they reduce
legend travel. A risk table, subgroup table, sample count, or event count is shown only when it is
supplied and bound as a separate panel—not inferred from the plotted rows.

Load `references/clinical-curve-contracts.md` before planning survival, diagnostic,
agreement, calibration, or clinical interval figures.
