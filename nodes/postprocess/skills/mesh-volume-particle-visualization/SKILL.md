---
name: mesh-volume-particle-visualization
description: Design and audit HPC/simulation figures from CFD, FEA, electromagnetic, geophysical, medical-volume, particle/DEM, and transient mesh data. Use for OpenFOAM, CGNS, Fluent, VTK-family, EnSight, Exodus, PVD, XDMF, Gaussian Cube, volumes, vector fields, streamlines, deformation, slices, thresholds, and point clouds.
---

# Mesh, Volume, and Particle Visualization

Treat the source as immutable evidence. First inventory dataset blocks, timesteps,
point/cell arrays, component counts, coordinates, and units. If the requested
field, block, timestep, geometry meaning, or units cannot be established, return a
structured missing-information request; never choose a plausible field silently.

Choose the scene by the scientific question:

- spatial distribution: surface, slice, orthogonal slices, or a transparent slice stack;
- internal topology: clip, threshold, or isosurface with explicit values;
- transport/vector structure: seeded streamlines or sampled glyphs with recorded scale;
- structural response: deformed surface with explicit warp factor and undeformed context;
- volumetric anatomy/material: explicit opacity/blending plus an auditable slice view;
- particles/DEM: physical radius when supplied, otherwise disclosed screen-space points;
- transient results: explicit timestep/time value; use comparable camera and limits.

Use a perceptually uniform sequential map for magnitude, a diverging map only
around a meaningful center, and log color only with a positive disclosed range.
Percentile clipping is display-only and must appear in the manifest. Never imply
that streamlines are pathlines, that screen-space point size is particle radius,
or that a warped mesh is true-scale unless the warp factor is one.

Every render must record source-bundle hashes, reader, block/timestep, selected
arrays and locations, derived display fields, scalar range/scale, operation
parameters, sampling, camera/projection, backend version, and source immutability.

Choose export structure from the scene, not the requested suffix. An exact
planar slice, planar streamline field, or planar glyph field may be rebuilt from
the selected mesh objects as pure SVG/PDF geometry. Verify that SVG contains no
embedded image before claiming `pure`. A volume, particle cloud, transparent 3D
surface, deformation, or non-planar field is pixel-composited: deliver a
high-DPI TIFF/PNG and an explicitly labelled hybrid PDF/SVG with editable text.
If `vector_export_mode: pure` is requested for an unsupported scene, stop; do
not wrap a screenshot in SVG and call it vector.

Load `references/three-dimensional-contracts.md` before setting any scene. Load
`references/domain-scene-patterns.md` when selecting a representation for a
specific simulation domain. Load `references/solver-adapter-matrix.md` before
claiming support for any solver-native file or bundle.
