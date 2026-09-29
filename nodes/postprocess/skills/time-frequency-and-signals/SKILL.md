---
name: time-frequency-and-signals
description: Render display-ready spectrogram and time-frequency grids while prohibiting STFT, wavelet transforms, resampling, interpolation, filtering, window selection, or logarithmic conversion in the visualization node.
---

# Time-Frequency and Signal Displays

## Responsibility boundary

Experiment must compute the transform. Never calculate an STFT/wavelet transform, choose a window
or overlap, resample, interpolate missing bins, filter, normalize, convert to decibels, or estimate a
noise floor here.

## Native spectrogram

Use `spectrogram` only for a duplicate-free complete rectangular grid with explicit `time`,
`frequency`, and `value` roles. `time_frequency_contract` must declare `kind`, `value_scale`
(`linear`, `log10`, or `decibel`), `time_label`, `frequency_label`, and `value_label`. Optional
`frequency_scale` may be `linear` or `log`; log frequency coordinates must be positive.

The renderer sorts coordinates for display without changing values, uses a perceptually ordered
sequential colour map, rasterizes only the dense data layer in vector exports, and records grid
dimensions and colour limits. Missing cells, duplicates, non-finite coordinates, or negative linear
power/magnitude values fail closed.

## Stop conditions

Request upstream rework when given only a raw waveform, when transform parameters are required, when
the grid is incomplete, or when the numeric scale/units are ambiguous.

## Reference

Load `references/time-frequency-contract.md` when validating scale or grid semantics.
