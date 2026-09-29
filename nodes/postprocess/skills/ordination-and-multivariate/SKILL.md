---
name: ordination-and-multivariate
description: Select and render display-ready PCA, PCoA, NMDS, RDA, CCA, and other ordination score coordinates without computing decompositions, distances, loadings, confidence regions, or explained variance inside the visualization node.
---

# Ordination and Multivariate Displays

## Responsibility boundary

Accept only coordinates and method metadata already produced by Experiment. Never run PCA,
standardize variables, choose components, calculate distances, fit ellipses, estimate centroids,
or infer explained variance here. A biplot is not a score plot: do not draw loading arrows unless a
future native biplot contract explicitly binds precomputed loadings.

## Chart selection

Use `ordination` when the scientific question concerns separation, overlap, gradients, or sample
positions in upstream ordination coordinates. Bind the first displayed axis to `x`, the second to
`y`, and a declared categorical identity to `group`. Reject a generic scatter only when the
ordination method and axis semantics are explicitly available; otherwise request upstream metadata.

## Required contract

`ordination_contract` must declare `kind`, `x_label`, and `y_label`. PCA additionally requires
`x_explained_variance` and `y_explained_variance` as fractions in `[0,1]`; the renderer verifies
their sum does not exceed one. PCoA/NMDS/RDA/CCA must retain their own method-specific axis labels
and must not be relabelled as PCA.

Use `show_origin=true` only when zero axes are meaningful for the declared method. Group identity is
encoded redundantly; do not add covariance ellipses, hulls, arrows, or significance annotations
without explicit upstream geometry and a native contract.

## Stop conditions

Request upstream rework for absent method identity, missing PCA variance fractions, coordinates with
missing/non-finite values, requested loadings that are not supplied, or a request to recompute the
ordination from raw features.

## Reference

Load `references/ordination-contract.md` when choosing among PCA/PCoA/NMDS/RDA/CCA or reviewing an
upstream metadata package.
