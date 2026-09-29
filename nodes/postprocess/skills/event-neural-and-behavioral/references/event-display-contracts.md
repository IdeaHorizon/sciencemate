# Event and neural display contracts

For native event raster rendering require:

- one numeric event-time field and one trial/unit field;
- `event_contract.trial_order` containing every observed trial exactly once;
- `time_label` with units and a `trial_label`;
- optional group identity supplied per event, encoded by color plus line style;
- any zero/alignment reference explicitly named in the upstream contract.

A PSTH, rate curve, spectrogram, tuning curve, or population trajectory is native only when its
display-ready coordinates already exist upstream. Binning, kernel choice, normalization, baseline,
alignment, dimensionality reduction, and uncertainty are Experiment outputs with lineage. Do not
connect across missing time intervals or silently reorder trials to make a pattern look cleaner.
