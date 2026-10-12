# MFF101 setup routing implementation plan

**Goal:** User selection of LightField routes to position 2; Andor routes to position 1, with editable global mappings.

**Architecture:** Keep all flipper USB calls on the existing MFF101 worker. Add a verified route operation used asynchronously by explicit combo activation and synchronously by spectrometer worker preflight. Mock sessions and passive state restoration never route hardware.

**Tech stack:** Python, PySide6, existing pylablib adapter.

**Spec:** User requests in this conversation: editable Settings light paths; automatic setup selection routing; authorized real-device tests.

## Tasks

- [x] Add regression tests in `tests/test_mff101_routing.py`: defaults/persistence, reversed mapping, unique discovery, actual readback, failed motion, mock bypass, passive restore, measurement lock, and worker preflight ordering. Initial seven tests failed for the expected missing APIs before implementation.
- [x] Extend `utils/config.py` with `auto_route=True`, `andor_position=1`, `lightfield_position=2`. Keep free-text labels descriptive. Settings adds enable checkbox and explicit position dropdowns; no motion when editing.
- [x] Extend `controllers/mff101_controller.py` with `route_for_setup(backend, use_mock=False)` and `ensure_setup_position(backend, use_mock=False, stop_event=None)`. Read current position, move only if needed, verify endpoint. Reject invalid mapping and missing/ambiguous devices. Synchronous callers wait on worker completion without depending on GUI event delivery. Preserve shutdown retry behavior.
- [x] Attach flipper to LF6 controller in MainWindow. Route on combo `activated`, connection preflight, parked-session activation, and scan/acquisition preflight. Existing mock sessions determine simulation from actual identity. Prevent manual flipper actions while LF6 measurement owns hardware.
- [x] Run MFF101, setup switching, controller, scan, config and session-state tests. Review the scoped diff using the existing 6.1-sol xhigh reviewer. Review found the legacy Motion Sweep warning handler swallowed routing failures; typed `MFF101RoutingError` now propagates, with regression coverage.
- [ ] Exercise real flipper via setup selection handlers, verify LightField=2 and Andor=1, restore initial position and disconnect. Save commissioning results. Update README with Settings behavior.

No commit or pull request is requested. Preserve unrelated workspace edits.

## Validation notes

- Final relevant regression command: `.venv-pyside6-313/Scripts/python.exe -m pytest tests/test_mff101_adapter.py tests/test_mff101_control.py tests/test_mff101_paths.py tests/test_mff101_routing.py tests/test_spectrometer_controller.py tests/test_winspec_scans.py tests/test_config_persistence.py tests/test_session_state.py tests/test_main_window_layout.py tests/test_bfp_metadata.py tests/test_motion_sweep.py tests/test_power_sweep_filename.py -q` → 124 passed, 22 subtests passed (48.81 s).
- Settings widget rendered and visually checked at 760 × 760 pixels; position controls and explanation are visible.
- `test_sidebar_active_setup.py`: four existing output-route failures reproduced with the unchanged HEAD LF6 controller loaded in memory. They are unrelated to flipper routing; the same four fail with these changes.
- Real setup-routing test attempted at 2026-10-11 23:36:17 UTC: opening USB serial 37862922 returned D2XX `Device Not Opened`. No movement was commanded. Subsequent MFF enumeration returned an empty list; Windows PnP also had no matching serial. Evidence: `commissioning_evidence/mff101_setup_routing_37862922_20261011T233617Z.json`. Asked user to reconnect USB/power; actual automatic routing verification remains pending.
- README automatic routing documentation updated.

## Measurement-stage follow-up

The user confirmed that measurement drafts should route when executing the
selected step, not while editing. The existing shared scan preflight already
handles both Dual Gate and 2D-map engines. Added 14 full-flow integration cases
using the real engines/controllers with simulated USB and detector SDKs: both
Andor roles, outer setup sweeps, setup changes inside gate points, independent
maps, reversed Settings mappings, and mid-run route failure blocking the next
detector's acquisition. Setup picker help and map setup tooltips now explain
this behavior.

Fresh regression run: `tests/test_mff101_measurement_routing.py`,
`tests/test_mff101_routing.py`, `tests/test_dual_gate_setup_picker.py`,
`tests/test_dual_gate_multi_setup.py`, and `tests/test_megasweep_sequence.py`
→ 81 passed, 4 subtests passed (81.04 s). The two Andor map selection/restore
tests also passed. This follow-up did not operate real hardware.

## Bounded retry follow-up

User approved an initial movement plus one retry, with readback reconciliation
before any retry. Implemented in the adapter so manual and automatic routes
share one policy: at most two commands, a 5-second initial arrival window, and
a 10-second total deadline including any wait for pre-existing motion. Unknown
or moving position only polls; a known opposite endpoint permits the retry;
target readback succeeds without another command. Transport exceptions,
including communication timeouts, are never caught as motion timeouts. The
controller passes measurement Stop and shutdown cancellation to the adapter,
preventing another command after either request.

Added timed fake-USB tests and production Qt-worker integration tests that
exercise retry recovery, exhausted retries, late arrival, ongoing motion,
communication failure, cancellation, shutdown, and acquisition blocking.
The initial retry test run had nine expected failures and three existing
communication-behavior passes. After implementation the adapter/controller
suite had 63 tests and 22 subtests pass; worker/measurement integration added
20 passing cases. No real USB movement was used for retry fault injection.

Review identified that Stop during a persistent in-transit readback could wait
until the full motion deadline and consume the shutdown budget. Added failing
tests for persistent motion and cancellation after the second command, then
checked cancellation before and after every USB read. Stop now ends polling
after the active bounded USB call. The original production-controller shutdown
reproduction with simulated USB now closes on the first call; the review found
no remaining issues. The expanded retry/control group has 35 passing cases.

Final regression after the cancellation fix: MFF101 adapter/control/paths/
routing/retry/measurement tests plus spectrometer, WinSpec scan, config,
session-state, MainWindow, BFP, and Motion Sweep suites: **160 passed,
22 subtests passed in 57.29 seconds**. Python compilation and scoped whitespace
checks passed. Real USB retry behavior remains uncommissioned.
