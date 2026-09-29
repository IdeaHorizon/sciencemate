# Time-frequency display contract

Required grid semantics:

- every `(time, frequency)` pair occurs exactly once;
- all coordinates and values are finite;
- the table contains the complete Cartesian grid;
- `value_scale` describes already-supplied values and never requests a visualization transform;
- labels expose time, frequency, units, and the colour quantity;
- optional limits must contain every supplied value.

Window function, segment length, overlap, sampling rate, padding, transform family, detrending, and
calibration remain upstream provenance. They may be carried as metadata but are never inferred or
changed here.
