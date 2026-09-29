# Scientific image display contracts

Require source bit depth, channel meaning, pixel spacing/units, orientation and every display window.

- Microscopy: channel/LUT legend, scale bar from calibrated spacing, saturation visibility, identical
  comparison windows, and explicit z/time/slice selection.
- Radiology: modality, plane, orientation labels, window/level, slice identity and patient-safe
  annotation. Do not infer anatomy or laterality.
- Astronomy/remote sensing: coordinate frame, orientation, angular/spatial scale, stretch function,
  mask/no-data treatment and filter/band legend.
- Insets/montages: source region boxes, magnification relation, stable reading order and independent
  scale bars when scale differs.

Never overwrite raw pixels. Crops, flips, rotations, LUTs, gamma and contrast windows are display
operations with parameters and hashes. Keep scale bars and text as editable overlays where supported.
