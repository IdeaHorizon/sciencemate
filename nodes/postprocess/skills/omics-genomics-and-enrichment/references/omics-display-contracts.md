# Omics display contracts

- `axis_semantics` for volcano names both already-computed display coordinates. Significance must
  be finite and nonnegative. Thresholds and labels must be supplied, not selected by the renderer.
- `genomic_contract` uses `coordinate_kind: cumulative_genomic_position`; `chromosome_order`
  covers every observed chromosome once, and chromosome coordinate ranges cannot overlap.
- `ma_contract` supplies numeric `center_line`, `x_label`, and `y_label`; abundance and log ratio
  are upstream results.
- `qq_contract` declares `reference: diagonal` and the reference distribution. Expected values
  strictly increase and observed quantiles are non-decreasing.
- `enrichment_contract` names x, marker-size, and color meanings plus sequential color direction.
  Term labels are unique; the visualization node does not rank, deduplicate, or filter them.

Never run normalization, differential analysis, association tests, p-value adjustment, enrichment,
or label selection here. If the requested figure requires those operations, request Experiment
rework and name the missing fields/contracts.
