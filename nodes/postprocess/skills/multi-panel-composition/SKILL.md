---
name: multi-panel-composition
description: Compose heterogeneous scientific panels into a coherent publication figure while preserving panel hashes, provenance, scale comparability, and final-size legibility.
---

# Multi-panel Composition

Define a grid, panel order, physical size, gutters, margins, alignment anchors, shared legends, and panel labels before composition. Retain each panel's source and output hashes in the composite lineage.

Use one deterministic SVG composition source for all requested deliverables. Preserve vector chart and schematic components as vector; keep evidence-bearing scientific images raster; keep panel labels editable. Put labels in a stable reserved band outside evidence rather than burning them into panel pixels. Never describe a raster panel wrapped in PDF/EPS as fully vector.

Use common scales, color limits, camera, or processing only when comparison requires them and the underlying semantics permit it. Otherwise state differences visibly. Align related axes and visual baselines, normalize typography and stroke weight, and keep label order consistent with the narrative.

Publication mode requires per-panel review before composition and another review of the final assembled figure. Any re-rendered panel invalidates the old composition and review hashes. Fail closed when the venue dimensions, minimum gutter, final-size label typography, required formats, or vector-structure contract cannot be met.

Load `references/layout-system.md` for heterogeneous grids, shared axes/colorbars,
panel sizing, alignment, gutters, reading order, and final-size typography.

## 语义自查清单（渲染后逐条过）

- 一段代码完成整个 composition（subplots/gridspec）；每个 panel 的源数据在 caption 中逐面板写明。
- 跨 panel 一致：字体族/字号、色板、线宽、图例顺序、背景色；共享轴时刻度对齐。
- panel label（A/B/C…）在专用留白带，不压数据、不压标题。
- 期刊栏宽约束下核对最终物理尺寸与每 panel 的最小可读字号（≥5-6 pt）。
