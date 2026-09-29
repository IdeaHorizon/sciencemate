---
name: electronic-structure-visualization
description: Render and audit DFT and quantum-chemistry electronic outputs, including VASP/pymatgen band structures and Gaussian/cclib orbital or vibrational spectra, with explicit energy reference, units, spin, and k-path semantics.
---

# Electronic Structure Visualization

Treat eigenvalues, orbital energies, and frequencies as immutable source values.
Require source-code identity, parser format, physical unit, energy reference, and
spin semantics. For a band structure, require the explicit KPOINTS or equivalent
high-symmetry path when path labels are claimed; otherwise label the horizontal
axis as k-point index. For molecular orbitals, distinguish occupied/unoccupied
levels using parsed HOMO indices. Never infer a band gap, align to VBM, broaden a
DOS, or calculate a spectrum merely to improve the picture.

Use an editable vector output for lines, levels, ticks, reference energy, and
labels. Record parser, file hashes, reference shift, number of plotted source
values, and that interpolation was not applied.

Load `references/electronic-contracts.md` for accepted VASP, pymatgen, and cclib
contracts and stop conditions.

## 语义自查清单（渲染后逐条过）

- 高对称点标签用上游给的路径；同一倒空间距离上的重合标签用 `|` 连接（如 `X|K`），不是缺陷。
- Fermi 能级/参考能级画在上游声明的能量处并标注；能量零点的选择写进 caption。
- 自旋通道共存时用颜色**加**线型双编码（色盲安全）；本征值几乎相等导致曲线重叠是数据事实，不要为分开而移动数值。
- 带结构与 DOS 并排时共享能量轴且刻度对齐。
- k 路径、单位（eV）、展宽参数全部来自上游 metadata —— 缺了就 request_upstream_rework，不猜。
