# Clinical curve and interval contracts

| Form | Required upstream content | Native hard stops |
|---|---|---|
| forest | unique row label, estimate, lower/upper interval, interval kind/level, effect measure, numeric null | incomplete/reversed interval; nonpositive ratio interval on a log axis |
| survival | numeric time, probability, estimator kind, optional group, optional lower/upper, optional boolean censor field | probability outside `[0,1]`; increasing survival; declared censoring without a bound censor field |
| ROC | precomputed FPR/TPR, curve kind, `[0,1]` ranges, diagonal reference | decreasing TPR as FPR increases; missing/nonfinite coordinates; AUC calculation request |
| precision–recall | precomputed recall/precision and `[0,1]` ranges | missing/nonfinite coordinates; threshold or AP calculation request |
| calibration | unique increasing predicted probability, observed probability, `[0,1]` ranges, diagonal reference | duplicate bins; binning/recalibration request |
| Bland–Altman | precomputed pair mean/difference and at least three distinct horizontal reference lines | computing bias/limits here; fewer than bias plus two agreement limits |

Do not infer whether an interval is CI, credible interval, SD, SE, or range. Do not place two effect
measures on one axis. Censor marks are observations, not events; they require a supplied field.
Risk tables and subgroup statistics require their own upstream rows and lineage.
