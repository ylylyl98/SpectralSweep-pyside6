# WinSpec temperature interlock implementation plan

**Goal:** Block InGaAs acquisition strictly above -100 C, on missing temperature,
or without temperature lock. PIXIS behavior and calibration remain unchanged.

**Architecture:** Guard preflight in WinSpecSetup and require a versioned guarded
XP acquisition command. The Python 2.7 bridge reads temperature in a separate COM
apartment during WaitForExperiment. A trip attempts Stop, rejects the frame and
stops the calling sequence. No temperature setpoint writes. Old bridges fail closed.

- [x] Test strict boundary, missing/invalid values, lock and continuity gaps.
- [x] Implement shared XP-compatible policy and desktop preflight/report checks.
- [x] Add guarded bridge acquisition and capability advertisement.
- [x] Review concurrency, run regression suites, and stage bridge upgrade.
- [x] Verify deployment status and report outstanding hardware validation clearly.

**Result:** 63 focused tests pass across separate Qt/non-Qt runs. Read-only live
GET_STATUS showed -100 C, locked, idle. Running XP bridge does not advertise guard
version: deployment is NOT active. VMware guest process access requires guest
credentials unavailable to this session. Upgrade files staged under
`XP_TRANSFER\temperature-interlock-update`; user must run the updater inside XP
with existing bridge closed, restart bridge and SpectralSweep. Real in-exposure
COM temperature readback still needs verification after deployment.

**Validation:** pytest tests/test_winspec_adapter.py tests/test_winspec_temperature_guard.py
tests/test_winspec_bridge_guard.py tests/test_winspec_integration.py. Hardware
read-only probes are permitted; do not warm the detector or change cooling to test.
