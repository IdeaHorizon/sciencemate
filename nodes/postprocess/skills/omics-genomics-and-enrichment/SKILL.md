---
name: omics-genomics-and-enrichment
description: Design genomics, transcriptomics, proteomics, association, differential, and enrichment figures from upstream-computed domain coordinates with explicit thresholds and biological labels.
---

# Omics, Genomics, and Enrichment Figures

## Scientific boundary

The input must already contain adjusted or unadjusted significance coordinates, effect or fold
change, genomic coordinates, abundance/log-ratio values, or enrichment results. Never compute
p-values, multiple-testing correction, fold change, cumulative chromosome positions, gene-set
enrichment, pathway overlap, normalization, or filtering in this node.

## Visual grammar

- Volcano: display-ready effect and nonnegative significance axes; threshold lines are upstream.
- Manhattan: cumulative non-overlapping chromosome coordinates and explicit chromosome order.
- MA: upstream abundance/log-ratio with a declared center and axis meanings.
- QQ: upstream expected/observed quantiles and a declared reference distribution/diagonal.
- Enrichment dot: unique term, effect, size, and color meanings; size mapping is display-only.

Alternate chromosome color plus position, and group color plus marker/shape, so identity is not
hue-only. Prefer selective direct labels supplied upstream; do not choose “top genes” or “top
pathways” inside rendering. Preserve gene/pathway identifiers exactly and disclose transformed
axis meanings rather than applying another log transform.

Load `references/omics-display-contracts.md` before planning a named omics figure.
