---
name: visual-intent-router
description: Normalize explicit or vague scientific visualization requests, choose the asset family and export defaults, and identify missing information before writing render code.
---

# Visual Intent Router

## Workflow

1. Preserve explicit instructions: requested chart, encodings, dimensions, formats, panel order, and style constraints are requirements unless they conflict with scientific integrity.
2. Convert vague requests into a visual question: comparison, relationship, distribution, temporal change, spatial pattern, structure, mechanism, or composition.
3. Read the request's `purpose` (exploration / diagnostic / publication / presentation / interactive / report) only to pick export defaults — physical size, DPI, formats. It is not an identity and no check reads it. Publication purposes mean journal column widths and >= 300 DPI raster output from the start.
4. Identify the asset family: quantitative chart, scientific image, spatial field, 3D/volume, molecular/material, schematic, or composite — then load that family's skill for conventions and its semantic self-check list.
5. Ask upstream (`request_upstream_rework`) only for scientific semantics that cannot be inferred safely, such as units, uncertainty meaning, sample unit, coordinate system, or molecule representation. Data *shape* problems are yours to handle in code.

## Boundaries

Do not parse logs, clean records, aggregate measurements, remove outliers, recompute statistics, smooth data, or infer missing units. Those are Experiment responsibilities. A visualization may derive only display geometry or reversible presentation transforms, visible in the render code.

## Output

There is no typed brief. Your working notes (chosen form, rejected alternatives, open questions) live as ordinary workspace files or in the final caption; the deliverable is the `figure` record minted by `render_figure`, which carries the provenance binding and findings.
