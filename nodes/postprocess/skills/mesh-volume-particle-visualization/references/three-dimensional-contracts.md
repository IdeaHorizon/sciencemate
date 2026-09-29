# Three-dimensional rendering contracts

## Readiness contract

Require an immutable run-scoped file and declare:

- coordinate system and length unit, or explicit `dimensionless` / `source_not_encoded` status;
- scalar/vector names, array location, component count, and field unit status;
- block/component for multi-block data;
- timestep or physical time for transient data;
- operation and all operation-specific parameters.

Missing semantics are not rendering defaults. Return the exact missing metadata
paths and list discoverable block, time, and array candidates.

## Display-only operations

- `surface` / `mesh`: preserve topology; do not decimate implicitly.
- `slice`: record plane origin and normal.
- `orthogonal_slices`: record x/y/z positions and keep an orientation cue.
- `slice_stack`: record axis, count, opacity, and scalar limits.
- `isosurface`: record every contour value and any cell-to-point conversion.
- `threshold`: record the retained scalar interval and retained cell count.
- `clip`: record origin, normal, and inversion.
- `streamlines`: record seed geometry, direction, integrator, step/length limits, and tube radius.
- `glyphs`: record deterministic sample count/index policy, vector field, and glyph factor.
- `deformed_surface`: record displacement vector, warp factor, and undeformed overlay policy.
- `volume`: record opacity transfer function, blending, shading, and color limits.
- `particles`: record original/rendered counts, sampling, screen point size, and opacity.

Vector magnitude, Cartesian component, and radial distance may be created only as
lineage-recorded display arrays on an in-memory copy. Gradients, vorticity,
stress, strain, particle density, interpolation, smoothing, and scientific
reconstruction belong upstream unless already supplied.

## Visual integrity

Use an orientation triad and an outline/context mesh when it makes the selected
plane or subset interpretable. Prefer orthographic projection for measurement
and cross-panel comparison. Perspective is acceptable for shape communication
when explicitly requested. Keep colorbars readable at final size and label units
honestly; use `source units (not encoded)` rather than guessing.

Transfer functions and transparency can manufacture apparent structures. Pair a
volume or heavy-opacity scene with slices when an internal claim must be audited.
The VLM may judge occlusion and legibility, but only the deterministic manifest
can establish field, block, timestep, scale, and source correctness.

## Export structure

- `pure`: allowed only when the exact selected planar geometry is emitted as
  vector triangles, line segments, arrows, axes, colorbar, and text. An SVG
  `<image>` element invalidates the claim.
- `hybrid`: required for volume ray casting, particles, transparency-heavy or
  non-planar 3D scenes, and scenes above the declared vector primitive cap.
  Record the rasterized layer role and DPI; retain editable wrapper text.
- Always include a PNG preview. Include TIFF at 300 DPI or higher when the
  scientific data layer is necessarily raster.
- Record requested and actual mode, vector formats, primitive count for pure
  scenes, fallback reason for automatic hybrid output, and file hashes.

The VLM reviews visible quality only. Pure-versus-hybrid status is a mechanical
file-structure and provenance decision and must never be inferred from appearance.
