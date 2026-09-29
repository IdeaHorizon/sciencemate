# Spatial field and map decisions

- Geographic maps require CRS, projection, extent and coordinate units. Choose projection from the
  comparison task; do not imply preserved area or distance when it is not preserved.
- Scalar fields require coordinate arrays, units, mask/no-data value, normalization and color limits.
- Diverging color requires a declared center. Phase/direction requires a cyclic map. Missing values
  must not share the zero color.
- Contours require upstream levels or a complete declared grid. Interpolation and regridding are
  upstream operations unless explicitly authorized and provenance-recorded as display-only.
- Vector fields require x/y, u/v, units, sampling and glyph scale. Subsampling must be disclosed.
- Cross-panel comparison needs shared limits/projection/orientation. If those differ, label the
  difference beside the affected panel.
