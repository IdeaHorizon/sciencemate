# Temporal integrity decisions

- Require the time origin, unit/timezone when relevant, observation identity, and ordering semantics.
- Preserve irregular cadence. Numeric sorting is display geometry and must be recorded; never resample.
- Represent a missing response as a broken line. Do not drop it and connect its neighbors.
- Facet or directly label many runs before reducing opacity until trajectories become uninterpretable.
- Longitudinal data require subject/run identity; a population interval is not a subject trajectory.
- Step and survival curves require upstream event/censor semantics and precomputed coordinates.
- Event rasters require event times plus row identity/order. Spectra require x units and declared
  frequency/wavelength orientation; peaks and baselines are upstream outputs.
- Shared axes are appropriate for comparison; otherwise disclose differences visibly.
