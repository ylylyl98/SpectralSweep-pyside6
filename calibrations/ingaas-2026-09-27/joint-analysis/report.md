# InGaAs 300 lines/mm partial calibration

Accepted center settings: 1300–1500 nm, SideExit, SP-2-300i serial 23580208, same 512-pixel detector/mounting.

Independent center checks at 1350 and 1450 nm were excluded from the joint hypothesis search. Fourteen peaks within the shared supported pixel ranges give RMS 0.07713 nm and maximum absolute error 0.12140 nm.

This is NOT a full 900–1700 nm calibration or a calibration of all 512 pixels. Valid pixel ranges are 199–463 for centers 1300–1400 and 199–477 for centers 1400–1500 (at 1400 the current app selects the first interval). Other centers remain uncalibrated. Wavelength decreases as native pixel index increases.

The first-order per-frame matcher failed; a joint quadratic model over center and pixel resolved a dominant assignment. Best training hypothesis matched 49 peaks within 0.3 nm versus 35 for the next distinct candidate. This is evidence for the assignment, not a factory-certified absolute-accuracy specification. The independent validation is against the same Ne/Ar reference list, not a separate calibrated laser.

Reproduce with explore.py followed by build_validated.py from the repository root. JSON contains anchor/check evidence and source hashes. Load ingaas-300-validated-1300-1500.json using Calibration → Load calibration in the updated app. Import recomputes validation from evidence and applies only to matching WinSpec identity; PIXIS is untouched.
