# MCD2100 automatic ramp-table cache execution report

Date: 2026-09-13

Implemented in the shared working tree without committing. The existing
`read_ramp_tables_async()` owner request is reused for both manual Refresh and
the one automatic attempt arranged after a completed display telemetry cycle.
The attempt is generation-scoped and deduplicated; busy work defers it until an
owner-status or worker-finish boundary. Automatic completion stores report,
generation, wall-clock read time and elapsed duration without opening a dialog.
View opens only the cached report; Refresh is the compatibility-preserving
explicit request path.

The controller adds the generation-bearing `display_cycle_finished`
observation signal and an additive `OperationHandle.accepted` admission fact,
used to distinguish rejection from an accepted device failure. The existing
DEBUG request record also includes queue wait; adapter per-RPC DEBUG output
completes the agreed telemetry diagnostics. No SDK writes, mode operations, Stop calls, connections or
additional owner threads were introduced. Timeout and partial report handling
retain the existing owner-drain interlock. Disconnect/shutdown invalidate
pending automatic arrangements and stale generation callbacks cannot open a
dialog or replace current cache state.

New regressions cover duplicate connection/cycle notifications, busy deferral,
manual/automatic overlap, disconnect/reconnect invalidation, automatic
dialog-free completion, timeout interlock/drain recovery, and real controller
offscreen ordering through the existing gated fake adapter.

Final affected-suite validation:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv-pyside6-313\Scripts\python.exe -m unittest tests.test_mcd2100_panel tests.test_attodry2100_controller
```

Post-review focused panel/controller validation: **95 tests passed**, including
regressions for shutdown closing gates and same-generation latest-read failure
metadata. The concentrated Astra probes pass: **12/12** in
`astra_auto_cache_review.py` and **6/6** in `astra_auto_cache_recheck.py`.
The recheck covers stale drain timeout status preservation, late connection and
cycle callbacks after shutdown, disconnect historical status, same-generation
failure details in the cache dialog, accepted manual consumption, and accepted
device failure consumption without retry. No hardware, App launch, new
connection, computer-use, write operation, or commit was performed.

Independent Astra final verdict: **PASS**, with all six targeted recheck cases
independently passing. Earlier panel/controller/adapter validation was 145/145
before the final repair regressions were added. See the adjacent review report
for scope and evidence; no all-project test or real-device performance claim is made.
