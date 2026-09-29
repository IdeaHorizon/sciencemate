# Molecular and materials scene contracts

- Protein: structure/model, biological assembly, chain/residue/atom selection, representation,
  secondary-structure assignment source, surface/probe settings, ligand and color legend.
- Small molecule: input conformer, protonation/charge, bond-order authority, stereochemistry,
  atom labels and 2D versus 3D representation.
- Crystal/material: unit cell, fractional/cartesian coordinates, periodic replicas, orientation or
  Miller indices, occupancy policy, atom radii, bond rules and polyhedra definitions.
- Density/orbital/surface: field source, isovalue, sign colors, units, opacity and clipping.

Never infer bonds, protonation, assembly or periodic contact merely to make the scene look complete.
Prefer orthographic projection for geometric comparison; perspective is acceptable only when it does
not create false contacts. Include an orientation cue and legend for nonstandard colors.

## Native ASE projection contract

- Input is one run-scoped PDB, CIF, XYZ, POSCAR, or VASP coordinate file plus an explicit
  `metadata.coordinate_system`.
- A multi-frame file requires `spec.frame_index`; no frame, average structure, or representative
  conformer is selected implicitly.
- `spec.view_direction` is a finite non-zero three-vector; the default is a recorded isometric
  display direction. Projection is orthographic and never changes source coordinates.
- Bonds are absent unless `metadata.bond_contract` provides unique atom-index pairs, positive bond
  order, and an explicit zero- or one-based index convention. Covalent-radius bond inference is not
  permitted.
- A periodic cell is shown only when the source contains a non-degenerate cell and the request asks
  for it or the source is periodic. No supercell or periodic replicas are generated.
- Atom colors follow the recorded ASE/Jmol element convention and relative marker areas use recorded
  covalent radii. The output includes an element legend and projected x/y/z orientation cue.

Stop and request a heavier domain adapter for ribbons/cartoons, solvent-accessible surfaces,
electron-density or orbital isosurfaces, occupancy expansion, symmetry generation, bond inference,
polyhedra, trajectories without an explicit frame, or any representation not reducible to an atom
and cell projection.

## Native trajectory projection contract

- Declare topology and trajectory paths when separate. XTC/TRR/DCD never invent a
  topology; a self-describing LAMMPS dump may be used directly.
- Require `frame_index`, MDAnalysis `atom_selection`, coordinate system, source
  length unit, and view direction. Record parsed time and periodic-cell dimensions.
- Atom downsampling is deterministic, even-index, display-only, and reports both
  selected and rendered counts. It must not be described as a representative sample.
- `color_by` may use a parsed topology category such as residue name, segment,
  atom name/type, or element. Missing categories are labelled unknown, not inferred.
- The renderer does not unwrap, align, center scientifically, calculate contacts,
  infer bonds, calculate density, or compute a representative structure.
