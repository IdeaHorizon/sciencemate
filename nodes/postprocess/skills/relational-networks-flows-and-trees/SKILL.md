---
name: relational-networks-flows-and-trees
description: Design scientific Sankey/alluvial flows, node-link networks, clustering dendrograms, and rooted phylogenies from upstream-supplied topology, values, coordinates, branch lengths, leaf order, and semantic contracts. Use for pathway, interaction, lineage, cohort-flow, hierarchy, clustering-tree, or phylogenetic figure requests.
---

# Relational Networks, Flows, and Trees

## Scientific boundary

Consume only upstream-computed flows, edges, topology, merge heights, branch lengths, support,
groups, labels, and layout coordinates. Never infer a missing edge, run a graph layout, cluster
samples, reconstruct a tree, estimate ancestry, redistribute flow, or repair conservation in the
visualization node. Request upstream rework when any scientific or layout identity is absent.

## Selection

- Use Sankey for conserved directed amounts between stages.
- Use alluvial for upstream-defined cohort/category trajectories through ordered stages.
- Use network for supplied node-link topology with upstream node positions and positive weights.
- Use dendrogram for supplied hierarchical-clustering topology and merge-height geometry.
- Use phylogeny for a supplied rooted topology, cumulative branch distance, tip order, and optional
  upstream support/tip groups.

Prefer a simpler quantitative chart when the research claim is about values rather than
relationships. Refuse dense graphs beyond the native publication grammar instead of producing an
unreadable hairball.

Load `references/relational-contracts.md` before planning any named relational figure.

## Review

Verify exact node/edge/flow/branch counts, unique identities, label completeness, direction,
thickness-to-value fidelity, conservation policy, root/topology integrity, branch-length direction,
leaf order, label collision, node resolvability, non-occluding legends, and redundant group
encoding. Treat attractive but structurally false geometry as a major scientific failure.

## 语义自查清单（渲染后逐条过）

- **只消费上游拓扑**：edges/flows/branch lengths/cluster 归属全部来自上游 artifact；本节点不推断边、不聚类、不重建树、不修补守恒。
- Sankey/alluvial：逐节点核对 in-flow 与 out-flow；不守恒时不要静默调宽度 —— 在 caption/findings 里披露差额与来源。
- Dendrogram/phylogeny：分支长度轴有单位与标签；上游没给 branch length 就画等距拓扑并声明。
- Network：布局算法与随机种子写在渲染代码里（可复现）；节点/边的视觉编码（大小/颜色/宽度）逐项对应上游字段并入图例。
- 标签与连线不互相遮挡（机械审计会报 OB-TEXT-*，但语义上的"哪条边属于哪个标签"要自己看）。
