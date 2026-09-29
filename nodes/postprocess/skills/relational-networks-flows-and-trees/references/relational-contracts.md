# Relational Figure Contracts

## Contents

- Sankey and alluvial
- Network
- Dendrogram
- Phylogeny
- Native limits and failure policy

## Sankey and alluvial

Bind one row per upstream flow segment:

| Required role | Meaning |
|---|---|
| `flow_id` | Unique segment identity |
| `source`, `target` | Exact upstream node IDs |
| `value` | Strictly positive supplied amount |
| `source_x`, `target_x` | Declared stage coordinates |
| `source_y0`, `source_y1` | Supplied source-side ribbon interval |
| `target_y0`, `target_y1` | Supplied target-side ribbon interval |
| `source_label`, `target_label` | Complete unique node labels |
| `source_stage`, `target_stage` | Ordered declared stage IDs |

Optionally bind `group` for a fully supplied flow identity. Require `flow_contract` with matching
`kind`, `flow_provenance: upstream_computed`, `layout_provenance: upstream_computed`,
`coordinate_system: normalized_layout`, value meaning/unit, positive `thickness_scale`, exact stage
objects, `show_flow_values: true`, and an explicit conservation policy. Every ribbon thickness at both ends must equal
`value * thickness_scale`; intervals may not overlap or contain undeclared gaps. Do not compute
flows, stack order, conservation corrections, or missing stages here.

## Network

Bind one row per upstream edge: unique `edge_id`, `source`, `target`, positive `weight`, exact
`source_x/source_y` and `target_x/target_y`, plus complete source/target labels. Bind both
`source_group` and `target_group` or neither. Require `network_contract` with network kind,
topology/layout provenance set to `upstream_computed`, normalized coordinates, direction, weight
meaning, and explicit false declarations for isolated nodes, self loops, and parallel edges.

The native grammar does not infer layout or isolated nodes and does not collapse multi-edges.
Coordinates and label/group metadata must be consistent wherever a node repeats.

## Dendrogram

Bind one row per supplied parent-child edge: unique `edge_id`, `parent`, `child`,
`parent_height`, `child_height`, normalized `parent_y/child_y`, and `child_label` for leaves.
Require `dendrogram_contract` with hierarchical-clustering kind, upstream topology/merge/layout
provenance, `height_direction: increases_toward_root`, height meaning/unit, distance-axis label,
one root ID, and the complete ordered leaf-ID list.
Declare `leaf_order_direction` as `bottom_to_top` or `top_to_bottom`; the ordered IDs must follow
that direction monotonically in the supplied normalized y coordinates.

Every non-root node must have one parent; the tree must be connected and acyclic; internal nodes
must branch; parent height must exceed child height; leaf order must match supplied y coordinates.
Clustering and linkage computation remain upstream.

## Phylogeny

Bind one row per supplied branch: unique `edge_id`, `parent`, `child`, cumulative
`parent_distance/child_distance`, normalized `parent_y/child_y`, and `child_label` for tips.
Optionally bind upstream `support` and `child_group`. Require `phylogeny_contract` with phylogram or
chronogram kind, rooted topology, upstream topology/branch-length/layout provenance, branch-length
meaning/unit, distance-axis label, one root ID, and complete ordered tip IDs. If support is bound,
declare its meaning and numeric range.
`leaf_order_direction` is mandatory and removes the common ambiguity between bottom-to-top
Matplotlib coordinates and top-to-bottom tree-viewer conventions.

Do not reconstruct topology, infer branch lengths, reroot, ladderize, collapse clades, or estimate
support. The native renderer accepts identity labels only for tips and support only on internal
branches.

## Native limits and failure policy

The publication grammar fails explicitly above these limits: 32 nodes/80 ribbons/six stages for
flows; 40 nodes/120 edges for networks; 40 leaves or tips and at most 80 phylogeny nodes; six
redundantly encoded groups. Use a declared specialized adapter for larger or interactive figures.
Never silently omit, aggregate, bundle, jitter, relayout, or relabel supplied scientific entities.
