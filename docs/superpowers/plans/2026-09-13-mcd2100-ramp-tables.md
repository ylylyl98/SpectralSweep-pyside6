# MCD 2100 Read-Only Ramp Tables Implementation Plan

> **For agentic workers:** Use the installed `executing-plans` skill. User selected Astra planning, Luna implementation, Astra review. Implement these three tasks sequentially; do not start the separate telemetry-refresh plan.

**Goal:** Add explicit read-only inspection of current/default raw ramp tables through the existing attoDRY2100 App connection.

**Architecture:** The adapter produces bounded, partial-error-preserving reports; one owner-thread controller command provides normal watchdog and independent drain acknowledgement. A native panel action/dialog presents results and owns a temporary shared-workflow interlock.

**Tech Stack:** Python, PySide6, existing SDK adapter/controller/futures, unittest/pytest fake hardware.

**Spec:** `docs/superpowers/specs/2026-09-13-mcd2100-ramp-tables-design.md`

## Global constraints

- Preserve every existing working-tree change, including the 1.67 K fix and reviewed magnet preparation.
- Use the existing adapter, connection and owner QThread. Do not construct a new SDK device or socket.
- The only new vendor calls are getNumRampRates, getRampRate, getNumDefaultRampRates and getDefaultRampRate. No setRampRate, mode mutation, field set/start/Stop, rollback or automatic recovery operation.
- No real hardware reads or tests during implementation or review. Only a later explicit user button click triggers a real read.
- Display raw values without unit labels or preset inference. Record method, channel, requested index when applicable and error text.
- Keep the telemetry-refresh feature untouched; no new periodic reads.
- No commits, new frameworks or unrelated refactoring.

## Task 1: Bounded raw adapter report

**Files:** Modify `app/devices/attodry2100_adapter.py`; extend `tests/test_attodry2100_adapter.py`.

**Produces:** these immutable report contracts and adapter method (names can be adjusted consistently, not behavior):

```python
@dataclass(frozen=True)
class AttoDRY2100RampRow:
    index: int
    raw_range: Any = None
    raw_rate: Any = None
    error: str | None = None

@dataclass(frozen=True)
class AttoDRY2100RampTable:
    kind: str  # 'current' or 'default'
    reported_count: Any
    rows: tuple[AttoDRY2100RampRow, ...] = ()
    errors: tuple[str, ...] = ()

@dataclass(frozen=True)
class AttoDRY2100RampTables:
    channel: int
    monotonic_s: float
    current: AttoDRY2100RampTable
    default: AttoDRY2100RampTable
    interrupted: bool = False

# On AttoDRY2100Adapter:
def read_ramp_tables(self, *, cancel_event=None) -> AttoDRY2100RampTables: ...
```

- [x] Write failing FakeMagnet tests for counts and raw rows on a nonzero channel, asserting the intentionally different argument orders. Include different current/default counts to detect accidental reuse.

```python
self.assertIn(('getRampRate', (2, 0)), device.magnet.calls)
self.assertIn(('getDefaultRampRate', (0, 2)), device.magnet.calls)
self.assertEqual(report.current.rows[0].raw_rate, 0.000000123456789)
self.assertFalse(any(n.startswith(('set', 'start', 'stop'))
                     for n, _ in device.magnet.calls))
```

- [x] Run new tests to establish failure. Add a constant `MAX_RAMP_TABLE_ROWS = 32` near adapter constants; explicitly document it as a software bound. Implement only four getter calls, preserving raw count/range/rate and recording method/channel/index with errors. Do not run safety preflight or change modes merely to read settings.
- [x] Add tests for count zero; invalid counts None, True, -1, 1.0, string and 33; missing method; count exception in one table with the other succeeding; one row exception surrounded by successes; malformed row tuple; unconverted unusual raw values. No row requests after invalid count; requested indices exactly `range(count)`, with no extra probe or retries.
- [x] Add cooperative-cancel test: set event during a row call; preserve that returned row, mark interrupted and issue no following getters. If cancelled before the first getter return an interrupted report identifying both tables as not read. Exceptions from individual calls become table/row errors, allowing bounded remaining reads unless cancelled.
- [x] Run adapter suite, including existing preparation and 1.67 K tests. Confirm no SDK construction happens in read_ramp_tables, no added behavior in connect/read_snapshot and no changes to external SDK files.

## Task 2: Read-only controller operation with drain-safe admission

**Files:** Modify `controllers/attodry2100_controller.py`; extend `tests/test_attodry2100_controller.py`.

**Consumes:** adapter `read_ramp_tables(cancel_event=...)`. **Produces:** `Command.READ_RAMP_TABLES` and `read_ramp_tables_async() -> OperationHandle`. Reuse current command mailbox and owner; use a per-request cancellation event so a later read cannot clear an earlier request's cancellation.

- [x] Write EventGatedAdapter tests: getter invoked once on the same thread as connect/read; max concurrent SDK calls remains one; disconnected/preparing/recovery/shutdown/draining and existing pending requests reject table reads. A second table request rejects immediately. No constructor/connection/set/start/mode/stop/close call appears as a side effect of the read.
- [x] Implement atomic admission/register/enqueue under the existing lifecycle lock. Permit IDLE and ACTIVE with no pending owner work; ACTIVE is necessary after standalone preparation. The panel additionally ensures no scan workflow is active. Reject read admission during preparation/recovery. While a ramp read is queued/running/draining, reject new preparation and ordinary mutations and defer/reject disconnect/shutdown until drain without automatic Stop. Keep controller state and ownership flags unchanged for successful reports and ordinary row errors.

```python
handle = controller.read_ramp_tables_async()
self.assertTrue(adapter.entered.wait(1.0))
with self.assertRaises(AttoDRY2100StateError):
    controller.set_h_setpoint_async(.01).result(1)
adapter.unblock()
report = handle.result(1)
self.assertEqual(handle.wait_drained(1), report)
self.assertEqual(adapter.max_active, 1)
```

- [x] Keep the normal request watchdog; add READ_RAMP_TABLES-specific timeout handling that only sets its read-cancel event. It must not call request_stop, change mode recovery flags or close transport. The adapter returns a partial report after the in-flight getter returns; `_finish` retains the already-failed client future and resolves drained_future with this report. Preserve/restore prior IDLE/ACTIVE state after owner drain unless a later legitimate controller lifecycle transition supersedes it.
- [x] Add event-gated timeout test: first rows succeed, next SDK read blocks beyond watchdog; client gets AttoDRY2100TimeoutError promptly, request remains pending/draining, mutations remain blocked, and no Stop/close occurs. Release gate; drained result contains prior successes and interrupted=true, no subsequent SDK getters ran, state is usable and repeat explicit read can succeed. Test cancellation event isolation and queued timeout/cancel without SDK execution.
- [x] Ensure final state restoration occurs before exposing successful owner-drain acknowledgement, and pending-state admission uses synchronized request states rather than stale queued UI state signals. Run controller + adapter tests; existing preparation cancellation/shutdown tests must remain green.

## Task 3: Native result dialog, explicit action and verification

**Files:** Modify `ui/mcd2100_panel.py`; extend `tests/test_mcd2100_panel.py`; add concise operation notes in `README.md`. If needed create a small `ui/mcd2100_ramp_tables.py` containing only result-dialog rendering; do not refactor unrelated panel sections.

**Consumes:** `controller.read_ramp_tables_async()`, immutable report, `OperationHandle.result/wait_drained`. **Produces:** `read_ramp_tables_btn`, `read_ramp_tables()` slot, panel-owned `_ramp_tables_handle`, retained report and a nonmodal read-only dialog.

- [x] Add failing offscreen tests: creating panel, connecting, ordinary telemetry refresh and preparation never call read_ramp_tables; explicit button calls it once using the injected existing controller. Dialog has Current/Default tabs and read-only Channel/Requested index/Raw range/Raw rate/Error columns. Raw precision survives display; count errors and successful other-table rows appear together; no units, Fast or index semantics are invented.
- [x] Add `Read ramp tables` beside existing magnet telemetry controls. Guard button and slot against worker, external busy, applying-temperature, pending controller requests, mode recovery and disconnected state. Acquire existing panel shared-instrument interlock before submission, release on immediate failure only when no owned work remains. Start/Prepare/disconnect/temperature-apply slots reject while `_ramp_tables_handle` exists. Prevent existing telemetry/temperature-monitor callbacks from enqueueing extra reads during this bounded operation; do not alter refresh timing or build telemetry-refresh functionality.
- [x] Build/show nonmodal native dialog with `Current` and `Default` tables, count/error summaries and spec caveat. Store report on panel. Disable cell edits. Display raw values with str/repr without unit conversion; blank values for failed payloads must be accompanied by explicit error. Closing dialog leaves owner operation untouched; no Stop/cancel-scan/close-transport action.
- [x] Poll the handle without blocking GUI. When the client times out show timeout immediately while keeping shared controls locked; separately poll `wait_drained(timeout=0)` and catch concurrent.futures.TimeoutError as still-draining. On eventual drain render the partial report while retaining timeout text. Handle failed/cancelled drain terminally without claiming complete success. If not timed out, get report after both acknowledgements. Stale callbacks must check active handle identity; retain successful rows on errors.

```python
# Panel test separates client completion and owner drain.
fake_handle.timeout_client()
panel._poll_ramp_tables(fake_handle)
self.assertFalse(panel.start_btn.isEnabled())
self.assertTrue(panel._interlock_held)
fake_handle.complete_drain(partial_report)
panel._poll_ramp_tables(fake_handle)
self.assertIn('timed out', panel.ramp_tables_status.text().lower())
self.assertIsNone(panel._ramp_tables_handle)
```

- [x] Extend `_refresh_controls`, `_release_interlock_if_drained` and shutdown checks for the panel-owned ramp handle; no unlock on dialog close or client timeout. `shutdown` returns false while ramp operation drains; it does not send a magnet command on behalf of the diagnostic read. On completion restore normal enablement and emit shared-workflow release exactly once. Test slot-level guards, no real controller connection in test constructors, and no overlap with a running scan.
- [x] Document explicit-only reads, raw index/units caveat, bounded counts, partial errors and timeout/drain behavior. Note source system specs do not establish SDK units/mappings; do not add numeric conversions to the App. Actual ramp tables have not been read.
- [x] Run focused suites and prior feature regression suites with fake hardware:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
python -m pytest tests/test_attodry2100_adapter.py tests/test_attodry2100_controller.py tests/test_mcd2100_panel.py tests/test_magnet_preparation.py tests/test_mcd2100_workflow.py tests/test_mcd2100_batch.py -q
python -m compileall -q app/devices/attodry2100_adapter.py controllers/attodry2100_controller.py ui/mcd2100_panel.py
git diff --check
```

- [x] Inspect diff for newly introduced SDK calls (only the four getters permitted), preserve prior changes and report exact test results. Hand off to Astra for review; no hardware invocation, commits or telemetry implementation. Astra review focuses on argument ordering, raw fidelity, partial failures, admission/interlock races, timeout drainage and zero implicit mutations.

## Plan self-review

The three tasks cover adapter data/error semantics, controller ownership/watchdog and UI/lifecycle respectively. Index base is explicitly unknown; enumeration never expands beyond the stated count. No manufacturer unit/preset inference is encoded. Timeout uses independent client/drained results and preserves partial reports. Existing telemetry plan remains a later sequential stage.
