# InGaAs calibration diagnostic session

Status: no validated wavelength calibration was generated. Existing PIXIS calibration was not modified.

## Raw collection

1200 lines/mm: 900–1700 nm in 50 nm steps; app captures listed in existing-capture-manifest.json.
- [750nm,300][1][0]: [900.0, 950.0, 1000.0, 1050.0, 1100.0, 1150.0, 1200.0, 1250.0, 1300.0, 1350.0, 1400.0, 1450.0, 1500.0, 1550.0, 1600.0, 1650.0, 1700.0] nm; original counts, optics identity and pre/post temperature report saved in each JSON.
- [750nm,600][2][0]: [900.0, 950.0, 1000.0, 1050.0, 1100.0, 1150.0, 1200.0, 1250.0] nm; original counts, optics identity and pre/post temperature report saved in each JSON.

## Findings

- Acquisition completed without the earlier save-prompt/GetParam blockage during the verified runs.
- Existing independent single-frame matching reports ambiguous reference assignment or insufficient peaks. These are algorithm rejection reasons, not proof that the lamp/reference is wrong.
- No usable broad model or valid RMS is available. Raw capture JSON can be opened with Load saved raw calibration capture in Advanced settings; it is not an enabled wavelength calibration.
- Collection stopped after the current center to avoid more low-value exposures. Further work should test joint matching across overlapping centers with independent validation.

## App improvements on disk

- Embedded tab navigation preserves automatic calibration.
- Automatic captures no longer show manual assignment prompts.
- Automatic runs start at 1 second, one accumulation; previous acquisition controls restored at completion.
- Busy temperature display states queries are paused; idle display accommodates 10 second polling.
- Raw exposure metadata uses the acquisition snapshot.
- Tests: 41 passed in focused suite with one Qt Escape test run separately (passed); combined suite has a Qt native crash at that test.