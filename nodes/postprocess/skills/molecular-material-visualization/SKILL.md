---
name: molecular-material-visualization
description: Plan molecular, protein, crystal, and materials figures with explicit representation, periodicity, orientation, selection, and domain-tool provenance.
---

# Molecular and Material Visualization

Require a domain-native adapter and an explicit representation: atoms/bonds, cartoon, surface, density, unit cell, supercell, slab, polyhedra, trajectory frame, or another supplied convention. Record model/structure ID, chain or atom selections, periodic replicas, bond rules, orientation, camera, colors, and radii. The native `domain_adapter` renders an ASE-backed orthographic atom projection from PDB/CIF/XYZ/POSCAR/VASP, an exact periodic cell when declared, and only explicitly supplied bonds. The `mdanalysis_matplotlib` adapter reads declared GROMACS/LAMMPS topology-trajectory bundles and renders one explicit frame/selection without changing coordinates. Use an MCP/domain backend for cartoons, surfaces, density fields, polyhedra, or interactive scenes.

Do not infer bonding, protonation, charge, occupancy handling, biological assembly, or periodic images without declared semantics. Avoid decorative perspective that hides sites or creates false contacts. Include orientation axes, lattice directions, legend, and scale when relevant.

If the required ChimeraX, PyMOL, VMD, OVITO, ASE, or other approved adapter is unavailable, report a capability error instead of falling back to an unfaithful generic sketch.

Load `references/structure-contracts.md` for proteins, small molecules, crystals,
surfaces, periodic cells, slabs, electronic-density or atomistic scenes.
