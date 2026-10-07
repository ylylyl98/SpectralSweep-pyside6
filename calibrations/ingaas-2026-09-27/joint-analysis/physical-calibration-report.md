# InGaAs physical-model calibration — 300 and 600 lines/mm

Saved in the SpectralSweep WinSpec calibration list. No PIXIS/LightField/firmware calibration writes were performed. Read-only calibration values and original grating/center/exit were equal before and after the acquisition session (settings-before.json and settings-after.json).

| Grating | Check centers (nm) | Matched check peaks | Check RMS (nm) | Maximum (nm) |
|---|---|---:|---:|---:|
| 300 lines/mm | 925, 1175, 1425, 1675 | 47/47 | 0.17165 | 0.65611 |
| 600 lines/mm | 1100, 1300, 1500, 1700 | 35/40 | 0.15473 | 0.34939 |

Both models return all 512 pixel wavelengths for arbitrary center settings from 900 through 1700 nm. This is a physical-model prediction, including locations outside the directly measured data; RMS is measured on the listed check peaks, not a uniform error guarantee at every pixel/center. Five unassigned 600-line check peaks are retained in its provenance and excluded from the matched-line RMS; their identities remain unresolved. The acceptance threshold for this physical-model method is RMS <=0.2 nm, unlike the earlier local fits' maximum-error threshold.

Model uses the grating equation with effective focal length in pixels, detector offset/tilt, inclusion angle and rotation offset. It is our application-level model, not the proprietary IntelliCal implementation. Reference matching uses LightField Ne/Ar line lists, including first and second diffraction orders. The stored axis is first-order-equivalent wavelength; higher-order light can overlap at a given pixel. Raw spectra and identification details are retained. Pixel pitch is not independently verified, so fitted focal length is expressed in pixels rather than claimed as a measured millimeter dimension.

Scope: the same WinSpec InGaAs 512x1 detector and unchanged mounting, SideExit, SP-2-300i serial 23580208, matching optical profile and selected grating. The application checks that identity for each frame. Changing mounting/optics can invalidate the model. PIXIS continues using its own calibration. 1200 lines/mm is not assigned a 900-1700 model because its first-order theoretical upper bound is below 1700 nm.

Files:
- ingaas-300-physical-900-1700.json
- ingaas-600-physical-900-1700.json
- physical-300-evidence.json and physical-600-evidence.json
- model-checks/: newly acquired validation/training spectra and temperature boundary checks

Application config was backed up before importing; only lf6.winspec_wavelength_calibrations changed. Reconnecting WinSpec automatically enables saved physical models for the current optical profile; actual grating/device/exit matching remains per acquired frame. The previous automatic broad-calibration button still uses the older collection/matching algorithm: these two results were generated in this supervised session, not by that button. Do not rerun it expecting the same physical-model workflow.

## End-to-end app verification
After import/restart/reconnect, acquired one 10 s frame at center1321.7nm on300lines/mm. App displayed nm automatically. Export app-verification-1321_7nm.csv contains512rows; every wavelength matches the saved physical model (1174.132045-1428.399473nm).
