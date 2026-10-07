# WinSpec full-spectrum trial — NOT ACTIVATED

Implemented and run offline against the existing long-exposure dataset. No new
acquisition, instrument configuration, LightField calibration, or application
calibration setting was changed.

## Method

LightField VISNIR PI Neon reference line list; Gaussian broadened template;
multi-center polynomial wavelength map; per-frame positive Ar/Ne amplitudes and
linear background. This borrows the whole-spectrum-fitting idea, not the proprietary
IntelliCal implementation or manufacturer physical model. Broadening is fitted,
not removed from measured data. Three starting offsets converged to similar costs.
Reference wavelengths retain their stored numerical convention.

Training centers: 900, 1000, 1100, 1200, 1300, 1400 nm.
Held-out centers: 950, 1050, 1150, 1250, 1350 nm.
Center range is NOT a claim of fully validated wavelength coverage.

## Validation

Held-out whole-spectrum registration shift RMS: 0.339 nm.
This allows a diagnostic shift to measure mismatch; that shift is NOT applied
to the saved model. It is not independent line-position RMS or IntelliCal RMS.

Same two provisional reference peaks at 950 nm center:
- InGaAs trial axis residual RMS: 0.967 nm.
- PIXIS existing axis residual RMS: 0.276 nm.

|Center nm|Reference nm|InGaAs trial nm|Residual nm|
|---|---|---|---|
|950|965.77860|966.70697|+0.92837|
|950|912.29670|913.30158|+1.00488|
|1050|965.77860|967.17312|+1.39452|

The trial fails the previously selected 0.2 nm target. It is NOT enabled in
SpectralSweep. The two-camera figures are limited local diagnostics: candidate
line identities, broad/blended InGaAs shapes, differing peak estimators and
unverified air/vacuum conventions prevent treating them as vendor calibration RMS.

## Deliverables and limits

`trial-model.json`: fitted parameters, split, hashes, quality metrics.
`validation.json`: explicitly held-out candidate peak errors.
`trial-spectrum-fits.png`: measured versus predicted counts, original pixels.
`UNVALIDATED-center-*.csv`: diagnostic trial axes, only for 900–1400 nm centers;
original pixel/count pairs preserved, never silently activated or extrapolated.
Centers above 1400 nm were excluded from the fitted domain because useful line
coverage becomes sparse. No 900–1700 nm accuracy claim is made.

Run `fit_trial.py` then `validate_trial.py` with the project Python to reproduce.
Raw files remain at the paths recorded in `trial-model.json`.
