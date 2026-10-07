# Persistent spectrum connections

**Goal:** Keep LightField and one Andor detector online, selecting one acquisition backend without closing or warming the other.

**Approved design:** Separate connections, remembered Spectrum settings, switching locked during measurements, separate Shamrock reconnect, and shutdown checks covering inactive Andor.

**Architecture:** Preserve the shared LF6Controller API used by every scan. The SDK worker retains inactive sessions (setup, adapter, identity, experiments), restores them on selection, and closes only the requested session. One Andor role may be connected at once because the roles share Shamrock. Keep temperature polling and exit safety aware of parked sessions.

**Tech stack:** Existing Python, PySide6, unittest; no new dependencies.

## Work

- [x] Controller tests: retain object identity and settings across LightField/Andor switches, no close on switch, failure rollback, reject switches during scans, close every session at shutdown, inspect inactive camera temperature.
- [x] Worker implementation in controllers/lf6_controller.py: session registry, connection reuse, failure rollback, all-session shutdown and temperature monitoring. Preserve active API compatibility.
- [x] Instrument UI: available device selector plus Connect / Use selected action, show current and online devices; keep connection actions locked during warmup and measurements.
- [x] Spectrum UI: remember per-backend controls and restore on activation; retain prior acquired data with its existing frozen metadata and label it as previous acquisition after a switch.
- [x] Tests: run unittest discovery for controller, Spectrum, session persistence and temperature monitor. Review diff and fix findings before completion.

Run tests with `.venv-pyside6-313/Scripts/python.exe -m unittest discover -s tests -p <test_filename>`.
Hardware validation requires a later application launch; do not restart the running cold-camera session automatically.
