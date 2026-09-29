---
name: event-neural-and-behavioral
description: Design event-timing, spike-raster, neural, electrophysiology, and behavioral figures while preserving trial identity, time alignment, conditions, and upstream-derived rates or trajectories.
---

# Event, Neural, and Behavioral Figures

## Scientific boundary

Every raster row is one supplied event. Trial/unit order, time origin, alignment event, condition,
and time unit must be explicit. Never bin events, compute firing rates/PSTHs, baseline-correct,
smooth, align trials, reject trials, average subjects, or infer missing trial order in this node.

## Visual grammar

Use an event raster for sparse timing and a line/band only for an upstream-computed rate or summary.
Separate subject, unit, trial, and condition identities; do not treat repeated observations as
independent samples. Use color plus line/marker style for condition, and preserve visible gaps.
When panels share an alignment event, time limits, or y ordering, bind those choices explicitly.

Load `references/event-display-contracts.md` before planning event rasters, neural trajectories,
PSTHs, peri-event figures, or behavioral trial panels.
