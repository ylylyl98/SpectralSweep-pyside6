# MCD2100 telemetry refresh execution report

Date: 2026-09-13

Baseline revision: `e7e6b49fc4b6c20dfdc89f09f70363307d9b9715`
Pre-telemetry focused regression baseline: 184 tests passed.

Implemented in the shared working tree without committing:

- Bounded controller request diagnostics and adapter RPC timing records. Timing
  uses injected monotonic clocks in tests, records actual getter calls once,
  keeps original exceptions, and stores at most 256 entries.
- Display-only magnet and temperature request slots with same-generation caches,
  0.5 second freshness, generation invalidation, owner-drain lifetime, and
  subscriber timeouts that cannot cancel the shared owner request.
- Completion-based background display polling through the same broker, with
  magnet then temperature ordering and a one-second next-cycle schedule.
- Panel refresh coalescing, staged updates, separate last-success ages, monitor
  sharing, timeout/drain locking, and last-known disconnected wording.

The Astra review corrections are also applied: drain callbacks no longer take
the controller lock while a mailbox lock can be held; subscriber futures are
independent; lifecycle and generation checks precede display cache/join
decisions; drained slots are reconciled synchronously; background and monitor
cycles advance after owner drain; disconnect stops the scheduler; display
updates carry their original completion timestamp and generation; panel worker
completion resumes connected display polling; refresh controls distinguish
display work from control work and retain a device-waiting timeout message.
Diagnostics include source and cache/join disposition while remaining bounded.

Final targeted review correction: temperature-monitor completion now forwards
the owner handle's completion timestamp and generation (including late drained
subscriber results), so a cache hit cannot reset displayed age. An integration
panel regression covers this path with the real controller owner.

Generation validation is also applied to plain snapshots supplied with an
explicit generation, preventing stale monitor or delayed callback data from
updating the panel. A direct stale-generation panel regression covers this
ordinary-snapshot path.

Validation command:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv-pyside6-313\Scripts\python.exe -m unittest tests.test_attodry2100_adapter tests.test_attodry2100_controller tests.test_attodry2100_telemetry tests.test_mcd2100_panel tests.test_mcd2100_workflow tests.test_mcd2100_batch tests.test_magnet_preparation
```

Result: **202 tests passed** (the final targeted panel regression raises the
working-tree total from 201). `compileall` passed for the changed adapter,
controller, panel, and worker modules. `git diff --check` passed; Git reports
only its normal LF/CRLF conversion warnings. No hardware, app launch, new
connection, commit, or telemetry benchmark was performed.

The separate ramp-table diagnostic remains explicit-only as previously reviewed;
the telemetry work does not add automatic ramp reads or alter safety/workflow
fresh-read APIs.
