# Domain scene patterns

Use these as representation candidates, not automatic scientific conclusions.

| Domain/question | Primary scene | Required evidence | Frequent failure |
|---|---|---|---|
| External/internal CFD | seeded streamlines plus pressure/speed surface or cut plane | velocity vector, scalar unit, seed geometry, domain block | decorative streamlines with no seed/integration audit |
| Turbulence/combustion | slices or supplied vorticity/species isosurfaces | upstream-derived field and meaningful contour levels | deriving vorticity or clipping peaks in Postprocess |
| Structural FEA | deformed surface colored by supplied stress/strain | displacement vector, stress component, warp factor, units | hiding deformation exaggeration or mixing nodal/cell stress |
| Electromagnetics | field-line streamlines plus magnitude slices/glyphs | vector field, magnitude unit, seed near source geometry | arrows so dense that direction is unreadable |
| Heat transfer | temperature slices/surface, optional velocity streamlines | temperature and velocity fields, solid/fluid block identities | plotting the wrong block or mixing temperature scales |
| Geophysics/reservoir | clipped/thresholded property volume or layered slices | coordinate convention, depth sign, property unit, active-cell mask | treating inactive cells as zero or reversing depth |
| Cryosphere/ocean/climate | plan view with log/linear speed and sparse vectors | projection/coordinate meaning, mask, velocity unit | rainbow map, missing mask, unlabeled log scale |
| Medical volume | orthogonal slices and carefully specified volume transfer | voxel spacing, orientation, intensity semantics/window | anatomy hidden by transfer function or left/right ambiguity |
| DEM/particles | point/sphere scene colored by supplied particle property | radius semantics, boundary/periodic policy, sampling | screen point size presented as physical diameter |
| Cosmology/astrophysics | auditable particle cloud or supplied scalar volume | coordinate unit/status, periodic box, sampling | invented density estimate or opaque central saturation |
| Transient multiphysics | fixed-camera selected timesteps or montage | physical time/index and shared limits | default first frame or rescaled colorbar per frame |

For comparison panels, lock camera, projection, crop, scalar scale, limits,
transfer function, glyph factor, and sampling policy unless the caption explains
why a parameter differs.

Planar CFD streamlines, 2D spectral-element glyph fields, and planar heat-flow
slices are strong pure-vector candidates. FEA deformation, 3D electromagnetic
field lines, reservoir cutaways, volumes, and particle clouds are normally
hybrid outputs; request high-DPI raster data layers instead of pretending they
are editable paths.
