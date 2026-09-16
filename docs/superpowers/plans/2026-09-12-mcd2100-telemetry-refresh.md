# MCD2100 Telemetry Refresh Implementation Plan

> **For agentic workers:** Use the installed `executing-plans` skill task-by-task. User-selected handoff: Luna implementation, then independent Astra review in the main conversation. This side conversation does not dispatch agents. Track steps with checkboxes.

**Goal:** Reduce redundant display telemetry reads and make refresh progress and data age visible without weakening fresh safety checks.

**Architecture:** Keep SDK access on the existing owner QThread. Add a display-only request broker in the controller, used by manual refresh, automatic display polling and temperature display monitoring. Keep existing safety/workflow read APIs separate. Instrument existing calls and expose partial display updates.

**Tech Stack:** Python, PySide6 signals/QTimer, concurrent futures, monotonic clock, unittest and existing fake SDKs.

**Spec:** `docs/superpowers/specs/2026-09-12-mcd2100-telemetry-refresh-design.md`

## Global constraints

- Wait for main-conversation magnet preparation implementation and review to finish; do not edit its files concurrently.
- Preserve the 1.67 K change and final preparation/recovery/controller ownership contracts.
- No hardware calls, vendor SDK edits, second connection, parallel SDK access, commits or branch changes are required by this plan.
- Cache/coalescing is display-only; all existing safety and workflow reads remain fresh and independent.
- Do not change safety sampling intervals or replace full safety snapshots with partial display data.
- Keep request slots occupied until drain, not merely until client completion.
- No unrelated UI redesign, dependencies or fixed wall-clock performance assertions.

## Entry checks and repository map

- [x] Read the linked spec and final magnet-preparation spec/code. Inspect `git status --short` and preserve existing changes. Record the baseline revision and relevant test count in the execution report, not by modifying user configuration.
- [x] Inspect `ui/mcd2100_panel.py`: `refresh_telemetry`, `_poll_telemetry`, `_poll_temperature_telemetry`, `_monitor_applied_temperature`, connection callbacks, `_refresh_controls`, and shutdown.
- [x] Inspect `controllers/attodry2100_controller.py`: owner `poll_once`, request mailbox, timeout/client/drain futures, generation checks, preparation/recovery gates and shutdown. Do not assume line numbers from this plan remain stable.
- [x] Inspect `app/devices/attodry2100_adapter.py`: `read_snapshot`, `read_temperature_snapshot`, `read_field`, `_optional`, `_temperature_call`. Existing reads are approximately 19 serial RPCs for a full manual refresh when all methods exist.
- [x] Run baseline focused suites using `.venv-pyside6-313/Scripts/python.exe`. Resolve environmental errors before edits. Existing unrelated failures must be recorded explicitly.

## Task 1: Measure queue, request and RPC latency

**Files:** Modify adapter/controller; create `tests/test_attodry2100_telemetry.py` using existing fake fixtures or test-local subclasses.

**Interfaces:** Add a bounded diagnostic deque and `telemetry_diagnostics()` returning a copy on the controller. Use a small immutable record with `request_id`, `generation`, `group`, `source`, `queued_at`, `started_at`, `finished_at`, `drained_at`, `outcome`. Fields not reached on early failure are `None`. Adapter RPC records contain method name, start/end monotonic time and success/error category. Existing public read return types remain unchanged.

- [x] Write failing tests using an injected clock. Queue a blocked prior request, advance clock, execute telemetry, and assert queue time differs from SDK execution time. Failures must still produce diagnostics and retain their original exception behavior.

```python
# Hand-derived clock positions: enqueue=10, start=12, finish=15.
self.assertEqual(record.started_at - record.queued_at, 2.0)
self.assertEqual(record.finished_at - record.started_at, 3.0)
self.assertEqual(record.outcome, "succeeded")
```

- [x] Run the new tests and confirm failure before implementing records.
- [x] Add timestamps at actual mailbox enqueue/owner execute/finalization boundaries. Measure RPC duration around actual calls, including exception paths. Avoid double-counting `read_field()` when called within `read_snapshot()`.

```python
started = clock()
try:
    result = rpc()
except BaseException as exc:
    record_rpc(method, started, clock(), type(exc).__name__)
    raise
else:
    record_rpc(method, started, clock(), "succeeded")
    return result
```

Here `rpc` is a closure over the original SDK call; `record_rpc` appends one bounded diagnostic entry. Keep the existing exception translation outside this wrapper. Do not log arguments.

- [x] Test buffer eviction after 257 entries leaves 256; test retrieving diagnostics does not invoke hardware. Run existing adapter/controller tests.

## Task 2: Display-only coalescing and generation-scoped cache

**Files:** Modify controller; extend `tests/test_attodry2100_telemetry.py` and controller tests.

**Interfaces:** Add `read_display_snapshot_async(*, max_age_s=0.5)` and `read_display_temperature_async(*, max_age_s=0.5)`, returning the existing OperationHandle type. Existing `read_snapshot_async`, `read_temperature_snapshot_async`, preparation preflight and workflow reads retain fresh semantics. Two display slots and two successful-result caches live in the controller and are protected by its existing lock.

- [x] Write failing tests with an event-gated fake: ten display calls while magnet read is blocked cause exactly one adapter read. An ordinary safety read during that interval causes a separate read. Assert SDK calls still all share one thread.

```python
handles = [controller.read_display_snapshot_async() for _ in range(10)]
# Release fake gate and wait for all results/drain.
self.assertEqual(adapter.magnet_read_count, 1)
fresh = controller.read_snapshot_async()
fresh.result(1)
self.assertEqual(adapter.magnet_read_count, 2)
```

- [x] Implement the broker decision under the controller lock: reject invalid connection/lifecycle first; join an undrained display slot; otherwise return a completed display handle for a same-generation success aged <= max_age_s; otherwise enqueue one new display request. Validate max_age_s is finite and >=0. With zero age, skip cache entirely while still joining current display work.
- [x] Explicitly clear caches on disconnect, reconnect/generation change and terminal detach. Only successful current-generation requests may populate caches. Never let stale completion clear a newer slot: compare the request identity before clearing.
- [x] Test freshness at ages 0.49 and 0.51 with literal expected read counts. Test failure is not cached; reconnect never uses old cache; timed-out request remains the sole display slot until drain; late result cannot replace a newer generation's value.
- [x] Test joining a display result cannot cancel an operation owned by another subscriber. No display subscription may invoke magnet Stop on timeout. Preserve preparation cancellation/recovery restrictions.
- [x] Run controller, adapter and new telemetry suites.

## Task 3: Share background/manual reads and schedule after completion

**Files:** Modify controller polling and `ui/mcd2100_panel.py` temperature display monitor; extend telemetry/controller tests.

**Interfaces:** Preserve `set_polling_enabled(bool)`. Replace the background path's direct adapter read with requests entering the same display broker. Timer callbacks run on the appropriate QObject thread; SDK access remains owner-only. Use a single-shot 1.0 s completion-based schedule, not the existing 0.5 s repeating full-snapshot timer. Do not repurpose `poll_interval_s` if preparation/safety also uses it; introduce a separate named display interval default if configuration is needed.

- [x] Write failing tests: background read blocked, manual refresh joins it; background finishes immediately before manual refresh and its <=0.5 s cached magnet result is reused. Delayed timer processing must not enqueue catch-up work.
- [x] Each automatic cycle reads magnet then temperature via the broker, emits each result independently, and schedules the next cycle only when both requests have drained. Manual refresh arriving between groups joins/reuses that cycle's work.

```python
# Ten elapsed nominal timer intervals while one fake request is blocked.
self.assertEqual(adapter.magnet_read_count, 1)
# No second automatic cycle before completion + display_interval_s.
self.assertEqual(automatic_cycle_count, 1)
```

- [x] Stop the display scheduler during measurement/preparation/shutdown ownership and defer if control work is queued. Resume after ownership release, with no timer restarting after disconnect. Read-only recovery refresh follows final preparation controller gates; never open mutations.
- [x] Route the panel's applied-temperature display monitoring through `read_display_temperature_async` to share reads. Invalidate temperature display cache after successful manual target application so old target/status does not falsely report stabilization. Keep workflow temperature stabilization on its existing fresh API.
- [x] Test Stop retains priority over queued display reads and no full-snapshot polling is inserted into timing-sensitive acquisition. Test enabling/disabling polling repeatedly leaves only one schedule.
- [x] Run telemetry/controller and preparation/workflow suites.

## Task 4: Partial updates, refresh state and truthful data age

**Files:** Modify panel; extend `tests/test_mcd2100_panel.py`.

**Interfaces:** Keep `refresh_telemetry()` public. Store one local refresh-cycle token plus connection generation and both display handles. Use existing `telemetry_note` or a small native label for separate magnet/temperature ages. Do not rename existing widget attributes.

- [x] Write failing offscreen tests: repeated slot calls during refresh submit only one cycle; button text becomes `Refreshing...`; magnet label updates while temperature is blocked; temperature error preserves magnet data and clears busy only after drain.
- [x] Implement a slot-level guard as well as button disabling. Use display broker APIs; set busy before submitting. Immediate submission exceptions restore state. A second click joins/no-ops and never schedules an extra trailing cycle.
- [x] End a refresh only after both handles terminal/drained. Treat a client timeout awaiting drain as `Refresh timed out; waiting for device response`, and retain the guard. Do not poll only SUCCEEDED/FAILED/CANCELLED if final controller semantics expose TIMED_OUT_DRAINING separately.
- [x] Store last-success times from successful reads/cache entries; reusing a cached result must not reset its timestamp to the current click time. A local UI timer updates age text without hardware reads.

```python
# Fake magnet completes at t=10; temperature remains pending until t=13.
self.assertIn("1.25", panel.current_field.text())
self.assertFalse(panel.refresh_btn.isEnabled())
# At t=12 the displayed magnet age is 2 s, even after a cached refresh.
```

- [x] Ignore callbacks whose cycle/generation no longer matches. Disconnect labels values last-known and disables refresh. Reconnect clears stale state, starts a new cycle and accepts only new-generation results. Shutdown removes/invalidates local timer callbacks safely without cancelling another subscriber's request.
- [x] Test coexistence with final Prepare/Start/recovery button locks. No real Qt window connected to hardware is used. Run panel and telemetry suites.

## Task 5: Integration, documentation and Astra review

**Files:** Update the README telemetry section; create a short execution report next to this plan after implementation.

- [x] Add event-trace integration tests covering combined automatic/manual/temperature display consumers, safety freshness, blocked RPC and reconnect. Test that one coalesced complete display cycle invokes one magnet and one temperature group, rather than N groups for N repeated clicks.
- [x] Run focused tests on the final main-task interfaces:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv-pyside6-313\Scripts\python.exe -m unittest tests.test_attodry2100_adapter tests.test_attodry2100_controller tests.test_attodry2100_telemetry tests.test_mcd2100_panel tests.test_mcd2100_workflow tests.test_mcd2100_batch tests.test_magnet_preparation
.\.venv-pyside6-313\Scripts\python.exe -m compileall -q app/devices/attodry2100_adapter.py controllers/attodry2100_controller.py ui/mcd2100_panel.py
git diff --check
```

If the final preparation tests use another filename, run that actual module and record the substitution; do not skip preparation coverage.

- [x] Document the display-only 0.5 s cache window, completion-based 1 s interval, staged display, last-success age and diagnostics access. State that queue/RPC measurements identify device bottlenecks and no real-device speedup has been measured.
- [x] Luna hands the plan, changed files, exact commands/results, baseline revision and deviations to the main-conversation coordinator. Leave changes uncommitted unless separately requested.
- [ ] Astra performs independent read-only review of: no cached safety reads; single SDK owner; priority and no duplicate cycles; watchdog versus drain; reconnect generation handling; preserving main preparation/recovery; correct cache age; bounded diagnostics; and successful existing workflows. Review must examine source and tests, not merely accept the execution report.
- [ ] Luna fixes all critical/important findings with regression tests; Astra reviews the fixes. Main conversation reports actual test totals and hardware verification limits.

## Acceptance checklist

- [x] Ten concurrent display requests yield one underlying group read.
- [x] Background/manual/temperature-monitor consumers share eligible display work.
- [x] Fresh safety requests always cause their own validated read.
- [x] Data appears group-by-group and age reflects the original successful read.
- [x] No retry storm, catch-up polling or old-generation display updates.
- [x] Timeout does not release the owner or display slot before drain.
- [x] No regression in mode recovery, Prepare/Start, Stop or shutdown.
- [x] Per-RPC/queue timing is testable and bounded, with no hardware benchmark claim.

## Self-review

All requested first-release changes map to Tasks 1-4; integration and independent review map to Task 5. Fast/slow field groups remain explicitly out of scope until timings justify them. Existing method signatures are retained for safety consumers. New display methods are named consistently. Implementation waits for the main work to settle because the shared controller/panel files are currently changing.
