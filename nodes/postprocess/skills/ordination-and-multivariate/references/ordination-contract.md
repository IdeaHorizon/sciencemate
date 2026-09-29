# Display-ready ordination contract

| Method | Coordinates owned upstream | Required display semantics |
|---|---|---|
| PCA | component scores | component names and explained-variance fractions |
| PCoA | principal coordinates | distance definition and axis labels supplied upstream |
| NMDS | ordination coordinates | stress/model information retained upstream; do not call axes PCs |
| RDA / CCA | constrained axes | method-specific axis labels; no inferred environmental vectors |

The visualization node can change only reversible display geometry such as marker channels, legend
placement, physical size, and optional zero reference axes. It cannot calculate group ellipses,
centroids, convex hulls, PERMANOVA, loadings, variable vectors, or component selection.
