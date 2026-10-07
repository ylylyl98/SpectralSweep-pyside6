Superseded by physical-calibration-report.md; this file records the earlier diagnostic stage.

# Full 512-pixel calibration status

Status: NOT COMPLETE. No full-detector model has been enabled or added to application settings. The earlier 1300-1500 nm partial model remains a separate, limited result.

## Requested operating domain
Center settings 900-1700 nm, arbitrary intermediate centers, all pixels 1-512, distinct model for each physically capable grating. No wavelength clipping at 900 or 1700 for pixels outside the center range. PIXIS calibration must remain unchanged.

## Implemented software support
`full_detector_surface` is an empirical total-degree cubic model of center and pixel. Import recomputes the fit from assigned reference peaks. Training and check centers must be disjoint; each 100 nm center band needs evidence in detector edges (pixels 1-32 and 481-512) and interior, in both sets. The original 0.2 nm tolerance applies to every fit/check residual. Wavelength direction is checked analytically across the center/pixel rectangle and every returned axis is checked. This is empirical validation with a stated edge margin, not measurement of every continuous setting or every pixel.

The import and spectrum-axis paths support this model. The existing automatic collection/matching workflow has NOT yet been converted to generate it. Do not interpret its old broad-calibration success as full detector coverage.

## Real diagnostic acquisitions
2026-09-27, 300 lines/mm, SideExit, same detector and SP-2-300i. Saved raw spectra and boundary temperature checks in `longwave-probe/`.
- Center 1500 nm: 10 s, 7 candidate peaks.
- Center 1650 nm: 30 s, 6 candidate peaks.
- Center 1700 nm: 30 s, 4 candidate peaks.

At 1700 nm, three candidate peaks align with doubled visible Ne/Ar reference wavelengths to approximately 0.05-0.11 nm under the previous diagnostic mapping; a fourth differs by 0.23 nm. This suggests second-order light and is NOT independent validation of a new calibration. Order identities and peak blending must be established before using these peaks as calibration evidence. No assignment has been automatically certified.

## Hardware limit
For groove density g lines/mm, the grating equation imposes |m*lambda| <= 2e6/g nm. For 1200 lines/mm this is at most 1666.7 nm in first order, even before the instrument geometry restricts it. Therefore a first-order 900-1700 nm full-detector model is impossible for that grating. A readback of center=1700 is not evidence of optical validity.
Reference: https://www.newport.com/mam/celum/celum_assets/np/resources/MKS_Diffraction_Grating_Handbook.pdf

## Remaining work
Confirm first/second-order assignments, supplement edge and independent-center evidence for 300 and 600 lines/mm, and validate the full operating rectangle. Existing coverage gaps are in `full-detector-coverage.json`. Do not widen/crop a partial model or lower the tolerance to manufacture success.
