# Dedicated calibration workflow

Approved scope: remove calibration controls from Spectrum, provide a dedicated
Calibration tab with Prepare / Review lines / Validate and save steps. Keep PIXIS
calibration untouched and preserve independent-check gating. Reference: official
LightField 4.5 manual Fixed/Broad workflow; this is not the IntelliCal algorithm.

- Preserve capture, context validation, broad scan cancellation and saving signals.
- Fix shared grid cell, narrow table headers and long strings; scroll small views.
- Keep fitting options/history in advanced controls and display readable context.
- Test independent page ownership, failed validation and geometry, then render UI.
- No hardware acquisition or automatic activation of an unvalidated fit.

Implemented: top-level Calibration tab with three scrollable steps, shared
acquisition controls, Fixed/Broad selection, advanced fit settings, independent
check RMS/max error, and unchanged validation gates. Broad Start initiates raw
collection; it does not automatically identify reference lines or enable a model.
Spectrum's conflicting grid row is removed. Escape cannot hide an embedded page,
and pending calibration captures finish/save even when the user changes tabs.

Verification: 31 integration/broad/Spectrum tests and 11 main-window tests passed
in separate processes. Combined Qt run encountered a native access violation;
isolated main-window rerun passed. Light/dark previews at 12 pt inspected.
No live hardware acquisition performed. Restart the app to load the UI changes.


## One-click broadband implementation
Broad now defaults to automatic capture at 17 centers (900�1700 nm, 50 nm
spacing), bounded exposure retries, Ne/Ar pattern matching in both pixel
orientations, held-out peaks, and independent center validation. Analysis runs
outside the GUI thread. Saving requires an explicit successful persistence
callback; cancellation or validation/save failure restores previous application
state. Known failed checks cannot be dropped to manufacture a passing model.
The current grating alone is calibrated. Full wavelength coverage is not promised;
only validated shared pixel/center intervals apply. This is a conservative linear
pattern model, not proprietary IntelliCal. The optical search prior is specific
to this 512-pixel OMA installation. No new live calibration has been performed.
Normal temperature interlocks remain active; no temporary warm calibration
exception is implemented in this change.


## All installed gratings
Broad Start now enumerates LightField grating capabilities, switches each grating
on the SDK worker, verifies readback and waits for worker idle before capture.
Every passed model is saved immediately with its grating/context identity.
Matching/validation failure retains that grating's old model and advances; hardware
or temperature errors stop the batch. Cancellation retains completed saved models.
The final report lists each grating's result. No hardware batch has been run.
