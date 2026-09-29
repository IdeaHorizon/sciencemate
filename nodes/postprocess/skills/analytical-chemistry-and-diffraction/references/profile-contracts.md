# Analytical profile contracts

## Chromatography

Experiment owns detector processing, baseline policy, peak definition, integration, compound
identity, alignment, and normalization. Visualization receives retention time plus final detector
response and declares the separation method and signal type.

Optional feature labels use `feature_annotation_contract` with `kind=upstream_features`,
`coordinate_system=data`, `provenance=upstream_confirmed`, and 1–12 items. Every item supplies
finite `x`, `y`, `label`, and `feature_kind`, and its anchor must exactly match one supplied trace
coordinate. The renderer never interpolates, detects, identifies, or auto-repositions a feature.
The default display is a horizontal label above that exact anchor with a short leader. The renderer
may add a bounded y-axis headroom band, records that display-domain change, and must prove at final
size that every label remains inside the axes and intersects neither another label nor a data trace.

## Diffraction

Experiment owns background subtraction, Kα handling, calibration, phase matching, peak fitting,
crystallite-size/strain analysis, and coordinate conversion. Visualization receives the final
profile. For a 2θ axis, radiation identity is mandatory; for q or d-spacing, the supplied unit must
remain visible in the axis label.

Miller indices or phase labels are permitted only as the same upstream-confirmed feature items.
Their presence is not evidence that the visualization node performed phase identification.
