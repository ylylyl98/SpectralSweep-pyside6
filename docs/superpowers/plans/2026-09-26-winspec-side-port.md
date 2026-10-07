# WinSpec side-port acquisition implementation plan

**Goal:** Add a Spectrum acquisition option using the existing LightField spectrograph session and the XP WinSpec camera bridge.

**Approved design:** User approved connection, single acquisition, display and saving first. Use pixels until a validated side-port calibration exists; 512 pixels and 50 micrometre pitch are provisional detector configuration, not calibrated wavelength evidence. Preserve existing uncommitted work and cooling. Do not restart the running application or acquire hardware during development.

**Architecture:** A bounded TCP client implements the existing WXRQ/WXRS protocol. A composite setup borrows an already connected LightField session and owns only the WinSpec client. It routes optics exclusively to LightField and acquisition exclusively to WinSpec. Pixel units remain frozen with each spectrum and export.

**Tasks:**
- [x] Add protocol tests for fragmented replies, header validation, errors, image dtype/shape and cancellation, then implement `app/devices/winspec_adapter.py`.
- [x] Test and add `winspec_ingaas` connection/parking/dependency lifecycle to `controllers/lf6_controller.py`; add address fields and a device entry in Instruments.
- [x] Test pixel axis labels, no eV conversion, CSV/metadata units and backend switching; implement Spectrum display/export changes. Preserve nm paths. Keep incompatible references separate.
- [x] Run focused regression tests and a read-only XP probe. Review changes independently using requesting-code-review. Document setup and live verification still required.

**Validation:** `.venv-pyside6-313/Scripts/python.exe -m pytest tests/test_winspec_adapter.py tests/test_spectrum_sessions.py tests/test_spectrometer_controller.py tests/test_spectrum_panel_controls.py tests/test_lightfield_optics.py`. No live optical or camera writes in tests.

