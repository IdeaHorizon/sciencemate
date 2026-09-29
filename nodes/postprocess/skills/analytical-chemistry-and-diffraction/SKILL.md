---
name: analytical-chemistry-and-diffraction
description: Render display-ready chromatograms and diffraction profiles with explicit instrument, coordinate, signal, and radiation semantics while prohibiting peak detection, baseline correction, integration, smoothing, phase assignment, and unit conversion in visualization.
---

# Analytical Chemistry and Diffraction

## Responsibility boundary

The input trace must already be scientifically prepared. Never perform baseline subtraction,
denoising, smoothing, peak calling, integration, deconvolution, retention-time alignment, phase
identification, background subtraction, normalization, or wavelength/2θ/q/d conversion here.

## Chromatograms

Choose `chromatogram` only for an upstream chromatographic trace. Bind retention time to `x`, one or
more supplied detector responses to `y`, and an optional run identity to `group`.
`chromatogram_contract` must declare `kind`, `x_kind=retention_time`, `separation_method`,
`signal_kind`, `x_label`, and `y_label`. Each trace requires complete finite coordinates with unique,
strictly increasing retention time.

## Diffraction profiles

Choose `diffractogram` for a supplied diffraction intensity profile. Bind the declared scattering
coordinate to `x` and nonnegative intensity/counts to `y`. `diffraction_contract` must declare a
supported `kind`, `x_kind` (`two_theta`, `q`, or `d_spacing`), plus `x_label` and `y_label`.
`two_theta` additionally requires the radiation source because identical angles under different
radiation do not have interchangeable physical meaning.

## Design

Use a restrained continuous trace without per-point markers. Preserve every supplied coordinate.
Multiple runs use redundant colour and line style. Peak labels or phase-reference ticks are allowed
only when `feature_annotation_contract` declares 1–12 `upstream_confirmed` labels anchored to exact
supplied trace coordinates. Do not interpolate an anchor, detect a peak, assign a phase, or silently
move labels. Prefer horizontal labels above the exact anchor with a short straight leader. A bounded
display-only y-axis headroom band may be added without changing data values. Final-size label
collision, trace occlusion, panel overflow, or canvas overflow is a hard failure.

## Stop conditions

Reject duplicate coordinates, missing values, negative diffraction intensity, absent instrument
semantics, or requests that require any analytical preprocessing.

## Reference

Load `references/profile-contracts.md` for the exact upstream/display split.
