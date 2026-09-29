# Solver adapter matrix

Format recognition is not scientific support. A solver is supported only when
its native bundle can be read, its blocks/times/arrays can be inventoried, and
the requested field semantics are explicitly supplied.

## Executable local adapters

- OpenFOAM `.foam`: PyVista `OpenFOAMReader`; hash the complete case tree,
  require explicit region/block and transient time, preserve patch names.
- CGNS `.cgns`: PyVista `CGNSReader`; inventory bases/zones/patches and select or
  explicitly merge zones. Reader warnings about unsupported boundary-condition
  node types must be surfaced as limitations.
- Fluent `.cas`, `.cas.h5`, `.dat.h5`: PyVista/VTK Fluent readers; require the
  case/data pairing and explicit cell/face zone or phase. A public Fluent CFF
  room result has passed source-bundle hashing, exact cell-centered rendering,
  pure SVG/PDF export without cell-to-point interpolation, mechanical QA, and
  Minimax-M3 review.
- LS-DYNA `d3plot` state sequences: LASSO-Python read-only parser feeding the
  spatial renderer. Hash the root, numbered state files, and `actunits`; require
  explicit state and element family. A public Ansys falling-sphere/beam result
  has passed native solid-hexahedron state rendering and review. The current
  adapter does not silently flatten shell, beam, ALE, SPH, or erosion semantics.
- EnSight `.case`, Exodus `.exo`/`.e`, serial/parallel VTK family, PVD,
  Nek5000, XDMF: PyVista; hash every referenced bundle member and require
  block/time selection. Historical render evidence (pre-2026-09 pipeline) covered EnSight,
  Exodus geometry, `.pvtu` hemodynamics/FEA bundles, PVD, Nek5000, and an
  XDMF/HDF5 explicit-time compatibility fixture. Do not present the tiny XDMF
  fixture as a scientific-quality benchmark.
- Gaussian Cube `.cube`/`.cub`: VTK Gaussian cube reader; distinguish volumetric
  field blocks from atom geometry and require explicit block/isovalue semantics.
  A real total-electron-density cube has passed isosurface rendering, bundle
  hashing, mechanical validation, and Minimax-M3 review.
- STAR-CD ProStar `.vrt` + `.cel`: paired-file PyVista reader. Native geometry
  was exercised once by a since-retired compatibility fixture. This does not imply support for
  STAR-CCM+ proprietary results, regions, reports, or scenes.

## Separate domain adapters

- GROMACS/LAMMPS trajectories: MDAnalysis, not PyVista. Require topology when
  the trajectory is not self-describing, plus frame, atom selection, length
  unit, periodic cell, and category semantics.
- NetCDF/Zarr/GRIB atmospheric or ocean grids: xarray plus Cartopy, not PyVista.
  Require coordinate-variable bindings, projection, calendar, time/vertical
  selection, mask semantics, and field units. Do not regrid or smooth locally.
  Native WRF curvilinear `wrfout` coordinates and colocated T2/U10/V10 fields
  have passed exact time selection, projection, vector-thinning audit, pure
  vector SVG/PDF export, mechanical QA, and Minimax-M3 review. Staggered and
  terrain-following diagnostics still require an upstream display-ready field.
- VASP/pymatgen and Gaussian/cclib electronic outputs: electronic-structure
  renderer, not mesh rendering. Require energy reference, units, spin semantics,
  source code, and KPOINTS/high-symmetry path when applicable.

## Capability states

Use `verified_scientific` only after a real native scientific result passes
source profiling, planning, rendering, mechanical validation, packaging, and
replay/hash checks. Use `verified_compatibility` when only reader, bundle,
block/time, and geometry semantics have passed. Use
`reader_available_unverified` when a library advertises a reader but no fixture
has passed. Current explicit boundaries:

- `verified_scientific`: OpenFOAM, Fluent CFF, CGNS, EnSight CFD, Nek5000, PVD,
  serial and parallel VTK results, LS-DYNA solid-state d3plot, Gaussian Cube,
  GROMACS, LAMMPS, NetCDF/xarray, WRF colocated surface fields, VASP/pymatgen,
  and Gaussian/cclib within the tested operations;
- `verified_compatibility`: Exodus II geometry, STAR-CD ProStar mesh, and the
  current small XDMF/HDF5 transient fixture;
- `reader_available_unverified`: legacy Fluent `.cas/.dat` and untested
  solver/version variants;
- `unsupported`: STAR-CCM+ proprietary results, TCAD proprietary structures,
  LS-DYNA shell/beam/ALE/SPH/erosion-specific visualization, and WRF staggered-grid,
  terrain-following vertical-coordinate, or derived-diagnostic computation.

For an unsupported source, request an upstream neutral export only when that
export preserves the required zones, units, timestep, coordinate system, field
locations, and topology; otherwise request a dedicated adapter rather than
guessing.
