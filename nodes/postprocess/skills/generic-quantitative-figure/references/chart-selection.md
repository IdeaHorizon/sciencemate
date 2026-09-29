# Quantitative chart selection contract

Select from the scientific question and upstream roles, not from a chart-name keyword alone.

| Question | Preferred display | Required upstream roles | Do not silently do here |
|---|---|---|---|
| ordered change | line/step with observations | ordered x, response, optional group/interval | smoothing, interpolation, resampling |
| continuous relationship | scatter; density only if supplied | numeric x/y, optional group/model output | fit a model or calculate correlation |
| discrete comparison | dot/interval; bars only for supplied magnitudes | category, value, optional interval | aggregate row-level observations |
| raw distribution | strip/rug or supplied density/counts | observation, group, stable sample unit | compute KDE, bins, quartiles, or tests unless the request explicitly authorizes a display transform |
| estimate uncertainty | coefficient/forest | label, estimate, lower, upper, interval semantics | infer interval kind or null value |
| matrix | heatmap | row/column coordinates, values, matrix kind, colorbar label, optional declared center/limits | infer a meaningful center; cluster, normalize, interpolate, or impute |
| classifier evaluation | ROC/PR/calibration | precomputed coordinates and reference contract | derive scores, thresholds, AUC, or bins |
| agreement/diagnostic | Bland–Altman/QQ/residual | precomputed diagnostic coordinates and limits | fit, residualize, estimate limits |
| high-dimensional biology | volcano/MA/Manhattan/enrichment dot | precomputed domain coordinates and thresholds | calculate p-values, adjustments, fold changes, enrichment |
| event timing | event raster | event time, trial/unit identity, explicit trial order, optional group | bin, rate-normalize, align, or infer trial order |
| supplied densities | density ridge | group, value coordinate, upstream density, width scale | compute KDE, normalize curves, or drop incomplete coordinates |

Native specialist contracts:

- Manhattan: `genomic_contract.coordinate_kind` is `cumulative_genomic_position`; its unique
  `chromosome_order` covers every observed chromosome; chromosome coordinate ranges are disjoint;
  displayed significance is finite and nonnegative. The node never computes p-values or cumulative
  genomic coordinates.
- MA: `ma_contract` declares the numeric center line and both axis meanings. The plotted abundance
  and log-ratio coordinates are already computed upstream.
- QQ: `qq_contract.reference` is `diagonal`; expected coordinates strictly increase and observed
  quantiles are non-decreasing. The node does not choose or fit the reference distribution.
- Residual: `residual_contract` names the residual kind and numeric reference value. Residuals and
  fitted values are upstream outputs, never calculated here.
- Enrichment dot: `enrichment_contract` names x, size, and color meanings plus the sequential color
  direction; term labels are unique. The node only performs a recorded display-area mapping.
- Event raster: `event_contract.trial_order` contains every observed trial exactly once. Each row is
  a supplied event; no binning, rate calculation, or alignment is introduced.
- Density ridge: `distribution_contract.width_scale` is positive and every group supplies a complete,
  nonnegative density curve on strictly increasing value coordinates.

Hard stops:

- A named form is unsupported by a healthy backend.
- A visual role is missing or ambiguous.
- A bar would contain duplicate category/group rows and therefore require aggregation.
- A log axis contains non-positive displayed values.
- A line would connect unordered categories or cross an unrepresented missing interval.
- A dual axis, area, volume, or color encoding lacks explicit semantics.
- A reviewed matrix lacks `matrix_contract`; a declared correlation matrix is not square,
  symmetric, aligned by row/column identifier, bounded as declared, or has the wrong diagonal.
- A named domain form is missing its explicit semantic contract, contains incomplete display-ready
  coordinates, or would require this node to compute scientific statistics.

For categorical identity, use color plus marker/line style/hatch. For ordered magnitude, use a
perceptually ordered sequential map; use a diverging map only around a declared meaningful center.
