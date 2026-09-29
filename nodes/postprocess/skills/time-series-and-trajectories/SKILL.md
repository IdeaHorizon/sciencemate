---
name: time-series-and-trajectories
description: Design temporal, longitudinal, and trajectory figures while preserving cadence, missing intervals, run identity, and upstream uncertainty semantics.
---

# Time Series and Trajectories

Treat temporal order as data, not decoration. Preserve timestamps, irregular cadence, replicate or run identity, and explicit gaps. Do not connect across missing intervals when continuity was not observed.

Use lines for meaningful ordered paths, points for observations, bands only for upstream-defined intervals, and facets or small multiples when many trajectories would occlude each other. Direct labels may replace a distant legend when density permits. State time units and timezone or reference origin when relevant.

Smoothing, interpolation, baseline correction, resampling, and alignment are analyses. Render them only when already supplied as separate upstream fields with lineage; never create them silently.

Load `references/temporal-integrity.md` for irregular cadence, missing intervals, longitudinal
subjects, survival curves, event rasters, spectra, or many-run trajectory decisions.
