# Statistical graphic input contracts

Every interval needs center, lower, upper, interval kind, level when applicable, sampling unit, and
scale. Preserve asymmetry. State the null/reference value for forest or coefficient plots.

- Forest/coefficient: label, estimate, endpoints, reference value, effect-measure scale; do not infer
  ratio versus difference from values.
- Survival: precomputed time, survival estimate, optional endpoints and at-risk table; censor marks
  need supplied coordinates.
- Calibration/ROC/PR: precomputed curve coordinates and any reference line semantics; AUC and
  confidence bands are upstream values.
- Bland–Altman: supplied mean/difference coordinates, bias and agreement limits; do not compute them.
- QQ/residual/diagnostic: supplied theoretical/fitted and observed/residual coordinates.
- Multiplicity-heavy figures: thresholds and adjusted values must be upstream fields; disclose the
  threshold meaning rather than decorating it as a discovery boundary.

Prefer point-plus-interval displays. A bar with an error bar hides the sample distribution and should
be used only when the magnitude-from-zero encoding is itself the requested scientific message.
