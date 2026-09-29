---
name: uncertainty-and-statistical-graphics
description: Visualize upstream statistical estimates and uncertainty without recomputing them or conflating SD, SE, confidence intervals, credible intervals, ranges, and raw variation.
---

# Uncertainty and Statistical Graphics

Bind center and interval endpoints to explicit upstream fields. Label interval semantics and confidence or credible level when supplied. Never infer an interval type from column names alone when ambiguity remains.

Prefer estimates with intervals and raw observations over mean-only bars. Show the experimental or sampling unit honestly; repeated measurements are not independent replicates. Use asymmetric intervals when supplied and preserve transformed scales declared upstream.

Do not calculate summary statistics, confidence intervals, p-values, effect sizes, bootstraps, fits, or outlier exclusions. Request Experiment rework when the required statistical artifact is absent.

Load `references/interval-graphics.md` for forest, coefficient, survival, calibration,
Bland–Altman, diagnostic, or multiplicity-heavy figures.
