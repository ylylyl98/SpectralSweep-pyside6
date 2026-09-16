# Local merge review — 2026-09-16

Reviewed the uncommitted MCD 2100 preparation, display telemetry, ramp-table
diagnostics, and workflow handoff changes against main at `e7e6b49`.

Independent review found three issues, fixed before merging:

- Refused panel shutdown retained the closing flag and blocked magnet recovery.
  Refusal now reopens admission and preserves display settings for safe deferred
  restoration after recovery or owner drain.
- Retargeting active field control left the controller ARMED, which rejected
  read-only diagnostics. Display and ramp-table admission now accept ARMED,
  retaining the existing mutation, recovery, and pending-owner checks.
- Connection, disconnection, and temperature-apply polling did not handle a
  timed-out client whose owner later drained. They now release those handles
  after drain and display the client timeout.

Four new regression tests reproduced these failures before the fixes and passed
afterward. Independent re-review passed the four tests and found no remaining
blockers in the fixes.

Validation with `.venv-pyside6-313/Scripts/python.exe`:

- Initial full discovery: 863 tests, 1 failure in
  `test_session_restores_step_and_per_address_compliance_without_io`.
  This same Keithley test failed identically in a temporary archive of unchanged
  main: the expected manual SMU state omits the persisted `targets` dictionary.
  It is a pre-existing failure outside this change.
- Final affected suites: 248 tests passed across adapter, controller, telemetry,
  magnet preparation, MCD batch, panel, and workflow.
- Compilation of changed production modules and `git diff --check` passed.

No real-device acceptance was performed. The existing hardware verification
limitations remain applicable.
