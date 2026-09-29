# Electronic-structure contracts

## Band structures

- VASP `vasprun.xml(.gz)`: parse eigenvalues and Fermi energy with pymatgen.
  Supply the matching line-mode KPOINTS file before claiming high-symmetry labels.
- Pymatgen MSON JSON: decode to a BandStructure object; preserve its distances,
  k-points, spin channels, labels, and Fermi energy.
- `energy_reference=fermi` applies the recorded display shift `E - E_F` and draws
  zero as the reference. `absolute` preserves parsed energies and draws the parsed
  Fermi energy. VBM alignment requires an explicit upstream analytical result.

## Quantum-chemistry logs

- Gaussian, GAMESS, ORCA, and Q-Chem `.log`/`.out` require an explicit
  `scientific_format` and cclib parser success.
- Orbital-level plots use exact parsed `moenergies` and `homos`. Frequency-stick
  plots use exact `vibfreqs` and parsed IR intensities when present.
- Broadening, convolution, peak assignment, transition selection, and population
  analysis belong upstream unless supplied as display-ready results.

Stop if units, reference, spin meaning, source program, KPOINTS pairing, or parser
identity is missing. Never relabel a k-point sequence as a high-symmetry path.
