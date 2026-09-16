# MCD 2100 Magnet Preparation Implementation Plan

> **For agentic workers:** Use the installed `executing-plans` skill to implement this plan task-by-task. The user selected Luna implementation followed by Astra review. Steps use checkbox syntax. Do not create additional user-owned tasks or request another design approval.

**Goal:** Automatically prepare the attoDRY2100 before MCD and expose the same operation as an independent Prepare magnet action, with truthful readiness, cancellation and recovery states.

**Architecture:** Add read-only preparation preflight and high-level Driven preparation to the existing single-owner adapter/controller, with a separate cancellation path. Reuse a small engine coordinator from the continuous measurement worker and standalone preparation worker. Reuse native panel worker/thread signaling, preserving existing scan/detach behavior.

**Tech Stack:** Python, PySide6, dataclasses, concurrent futures, threading events, existing unittest/pytest tests.

**Spec:** `docs/superpowers/specs/2026-09-12-mcd2100-magnet-preparation-design.md`

## Global constraints

- Preserve the existing uncommitted 1.67 K temperature fix and its tests.
- Work on the existing `codex/mcd2100-magnet-preparation` branch; do not commit or operate real hardware.
- Use Python, PySide6 native widgets, the existing SDK adapter and single-owner controller. No new framework or UI restyling.
- Only `setDrivenMode(channel, True)`, existing field setpoint/start/stop operations and existing readback APIs may control the magnet. Never implement manual switch-heater, supply-current, persistent-mode rollback, ramp-rate or limit programming.
- Quench must be explicitly false. Field and magnet temperature must be finite and within the configured limits, capped by the existing hard 6 T and 7 K ceilings. Sample temperature is a separate existing control.
- Configuration and telemetry failures fail closed before further mutations. A precheck failure alone never sends a magnet Stop.
- A request acknowledgement is not physical readiness. UI messages and documentation must distinguish observed software readiness from an unverified physical transition guarantee.

## Repository map and current pitfalls

`app/devices/attodry2100_adapter.py`: `_preflight` currently rejects non-Driven before every set/start, `_motion_limits` owns configured ceilings, snapshot includes heater/lead field/HState. Do not replace these with worker hardcoded limits. `getLeadsHot` is informational current-presence telemetry, not temperature.

`controllers/attodry2100_controller.py`: owner serializes commands; `_Request` has separate client/drained futures. Default watchdog is SDK timeout + 2 s. Mailbox stop cancels queued mutations, and timed-out set/start currently auto-submit Stop. New mode preparation must be in mutation cancellation sets but excluded from automatic field Stop. Disconnect/shutdown and completed detach use `field_may_be_active`, which must not be overloaded to mean mode-transition uncertainty.

`app/engine/mcd2100_worker.py`: active continuous class starts around line 385; discrete class is legacy. Continuous `run` currently does temperature and optics/gates before first magnet check. `_snapshot_values` calls the legacy strict validator, requiring active field control. `_set_and_start` marks `_magnet_command_issued`; cleanup exempts only selected prefield failures and incorrectly stops on other read-only failures. `_wait_gate` uses five consecutive samples. Its endpoint-timeout exception deliberately avoids Stop; preparation needs its own timeout error.

`ui/mcd2100_panel.py`: `start` currently runs LF ensure_ready synchronously before creating the worker. `_Runner` supports generic callbacks and returns a dict. `_on_terminal` assumes every COMPLETED means disconnected measurement; standalone preparation needs separate handling. `shutdown` waits for worker but must also respect controller recovery/drain. MainWindow observes `run_state_changed` to lock shared instruments.

Existing tests: `tests/test_attodry2100_adapter.py` (FakeDevice/FakeMagnet), `tests/test_attodry2100_controller.py` (EventGatedAdapter), `tests/test_mcd2100_workflow.py` (scripted continuous fake controller/clock), `tests/test_mcd2100_batch.py`, `tests/test_mcd2100_panel.py`. Existing fake snapshot arrays assume no early reads; update fixtures explicitly rather than silently bypassing new production checks.

## Task 1: Read-only safety and bounded adapter mode preparation

**Files:** Modify adapter and `utils/config.py`; extend adapter tests.

**Interfaces to produce:**

```python
@dataclass(frozen=True)
class DrivenPreparationResult:
    snapshot: AttoDRY2100Snapshot
    mode_requested: bool

def preflight_magnet(self, targets_t=(), stop_event=None) -> AttoDRY2100Snapshot: ...
def prepare_driven_mode(self, *, timeout_s=300.0, poll_interval_s=0.5,
                        lead_tolerance_t=0.001, stop_event=None,
                        observe_only=False, on_mode_requested=None,
                        on_snapshot=None) -> DrivenPreparationResult: ...
```

Methods live on the adapter. `observe_only=True` means unresolved recovery: strict polling only, no mode request. Callbacks execute on the owner thread; no UI method is called directly. Optional injected adapter clock/sleep or a tiny overridable wait helper supports deterministic tests. Validate timeout/tolerance as positive finite and reject bool-as-number telemetry.

- [x] Add failing fake SDK tests for a read-only Persistent preflight, invalid/contradictory mode booleans, missing field-control boolean, NaN field/temp, quench, configured endpoint limits and stopped-before-preflight. Assert no SDK writes.

```python
device.magnet.driven_mode = False
device.magnet.persistent_mode = True
snapshot = adapter.preflight_magnet((0.01, 0.02))
self.assertFalse(snapshot.status.driven_mode)
self.assertFalse(any(n.startswith(('set', 'start', 'stop'))
                     for n, _ in device.magnet.calls))
```

- [x] Run these tests and confirm failure is the new missing API/behavior.
- [x] Extract common snapshot safety validation from `_preflight`. Keep ordinary `_preflight` requiring Driven; preparation preflight allows complementary valid mode booleans and inactive field control. Validate every requested endpoint against `_motion_limits`. Expose read-only preflight without using SDK mutation APIs.
- [x] Add `mode_prepare_timeout_s=300.0` and `mode_lead_tolerance_t=0.001` to `AttoDRY2100Config`; the controller uses config defaults and tests may override. Existing JSON loading remains backward compatible.
- [x] Add failing tests showing mode return acknowledgement alone never succeeds: mode booleans immediately become ready but heater=false, lead mismatch or HState unknown remains; third consecutive strict-ready snapshot succeeds. Add missing heater/lead failure, sample reset, already-ready no-write, timeout, stopped-before-call, cancellation during polling, and observe-only retry no-repeat-request tests. Fake `getLeadsHot=True` must succeed.

```python
# Script snapshots in a fake adapter; all use safe temperature/quench.
# Boolean ack, then incomplete heater, then three fully evidenced samples.
result = adapter.prepare_driven_mode(timeout_s=5, poll_interval_s=.01)
self.assertEqual([n for n, _ in device.magnet.calls].count('setDrivenMode'), 1)
self.assertTrue(result.mode_requested)
# Separately run observe_only=True with the same ready snapshots:
# zero setDrivenMode/setHSetPoint/startFieldControl/stopFieldControl calls.
```

- [x] Implement once-only `setDrivenMode(channel, True)` after a final cancellation check and common safety preflight. Invoke `on_mode_requested` immediately before call; wrap SDK exceptions as existing communication errors. Poll fresh snapshots with deadline and cooperative cancellation. Request is skipped when already Driven and no contradictory auxiliary evidence; unresolved recovery always uses strict three-sample criterion from the spec. Unknown/missing required evidence remains waiting until timeout; actual unsafe quench/limits/invalid essential telemetry fails immediately. Emit snapshots via callback. Do not use getLeadsHot as a safety predicate.
- [x] Run adapter tests. Verify ordinary set/start still reject non-Driven and existing 1.67 K tests pass.

## Task 2: Single-owner commands, cancellation, watchdog and recovery

**Files:** Modify controller; extend controller tests.

**Interfaces to produce:**

```python
def preflight_magnet_async(self, targets_t=()) -> OperationHandle: ...
def prepare_driven_mode_async(self) -> OperationHandle: ...
def cancel_magnet_preparation(self) -> None: ...
@property
def mode_recovery_required(self) -> bool: ...
```

Add commands `PREFLIGHT_MAGNET`, `PREPARE_DRIVEN` and states `PREPARING`, `RECOVERY_REQUIRED`. Preserve state and mode-recovery flags through cancellation and failures; use existing lifecycle lock for shared access. `PREPARE_DRIVEN` timeout is mode timeout plus one normal SDK request timeout/drain allowance, not the default short watchdog. Add a per-request timeout override to `_new_request` rather than globally lengthening all commands.

- [x] Add EventGatedAdapter implementations and failing tests: preparation calls all on one SDK thread; overlap max=1; >default-watchdog preparation can finish within mode deadline; ordinary submissions rejected while preparation owns the controller; cancel-before-SDK prevents mode mutation.
- [x] Implement owner execution for read-only preflight and preparation. Before invoking adapter prepare, record previous state; set PREPARING. The adapter callback marks mode uncertainty before SDK write; strict success clears it. Publish callback snapshots via existing signal. No uncertainty on pure preflight failure. Successful preparation returns to the prior field ownership state (or IDLE), without inventing ACTIVE ownership from a mode request.
- [x] Implement cancellation using a preparation event/unique operation token whose lifetime covers queued through drained state. Never clear that event for a new operation until the earlier one drained. Check it before mode SDK mutation and between reads. Cancel queued preparation via mailbox; reject duplicate preparation requests. request_stop/request_shutdown publish preparation cancellation, but mode uncertainty prevents them from issuing SDK Stop or closing transport.
- [x] Add timeout tests blocking SDK before/after mutation. On client timeout set preparation cancellation and TIMED_OUT_DRAINING; no auto request_stop. Keep pending-work lock until owner drain; queued setpoint/start/temp must not execute after late return.

```python
adapter.arm_gate('prepare_mode_mutation')
handle = controller.prepare_driven_mode_async()
self.assertTrue(adapter.entered.wait(1))
controller.cancel_magnet_preparation()
# Before release: SDK owner remains single and controller remains busy.
adapter.unblock()
with self.assertRaises(Exception):
    handle.result(1)
# drain can also report the operation exception; assert it became terminal.
self.assertNotIn('stop', [item[0] for item in adapter.calls])
self.assertTrue(controller.mode_recovery_required)
```

- [x] Implement recovery gating: while uncertainty remains allow read-only requests and prepare retry; pass observe_only=True on retry. Reject set/start/temperature mutations, disconnect, automatic Stop and shutdown with actionable state errors. Shutdown returns false, preserving QThread and adapter; no terminate. A successful recovery clears uncertainty and restores usable state. A timeout before an actual request still needs owner drain but not false permanent physical uncertainty.
- [x] Add tests for recovery strictness despite ready booleans, no second setDriven request, blocked close/shutdown retaining adapter, successful retry permitting set/start and shutdown afterward. Ensure failed shutdown does not leave a stale `_shutdown_handle` permanently blocking recovery; ensure stale `_stop_handle` does not poison next successful run.
- [x] Run controller and adapter suites. Tests clean up blocked fakes by releasing gates, recovering explicitly, then shutting down; assert zero leaked QThreads.

## Task 3: Shared preparation coordinator and standalone worker

**Files:** Create `app/engine/magnet_preparation.py` and `tests/test_magnet_preparation.py`.

**Interfaces to produce:**

```python
class MagnetPreparationCancelled(RuntimeError): pass
class MagnetPreparationTimeout(RuntimeError): pass

class MagnetPreparation:
    def __init__(self, controller, start_field_t, *, targets_t=(),
                 gate_t=0.001, poll_interval_s=.2, timeout_s=300.,
                 operation_timeout_s=180., cleanup_timeout_s=30.,
                 stop_event=None, phase=None, log=None,
                 clock=time.monotonic, sleep=time.sleep): ...
    def prepare(self): ...  # final fresh snapshot
    def request_cancel(self): ...
    # Public booleans: field_command_issued, mode_requested

class MagnetPreparationWorker:
    def __init__(self, controller, start_field_t, **preparation_options): ...
    def set_callbacks(self, *, phase=None, log=None, **ignored): ...
    def request_cancel(self): ...
    def run(self) -> dict: ...
```

Only controller APIs are used, never adapter/SDK attributes. Result dict includes `operation='magnet_preparation'`, status, error, cleanup_error, snapshot and recovery_required; no CSV paths or experiment metadata creation.

- [x] Add fake-clock tests for the expected sequence, both active/inactive field control, actual field already at Start with differing old setpoint, and final five-sample stability. Both endpoints are preflighted before mode mutation. Ensure no set/start until prepare-Driven handle acknowledges and drains.

```python
# Event names recorded by the fake controller:
self.assertLess(events.index('preflight'), events.index('prepare_driven'))
self.assertLess(events.index('prepare_driven.drain'), events.index('set:0.01'))
self.assertLess(events.index('set:0.01.drain'), events.index('start'))
```

- [x] Implement preflight -> prepare_driven -> fresh preflight/readback -> set/start if required -> five fresh safe in-gate readings. Always establish Start as verified target if old setpoint differs, even if actual field is currently in gate. Use controller-configured safety validation, not only field gating. Emit useful current/target status and retain snapshots for worker metadata callbacks if needed. Mode handle wait budget must accommodate controller mode timeout; ordinary handles retain their normal budgets.
- [x] Add cancellation tests at precheck, waiting mode, before setpoint, between setpoint and start, and while positioning. Cancellation calls cancel_magnet_preparation during mode phase. Set field_command_issued immediately before first field submission; after that use idempotent request_stop. Close the cancel-versus-submit race with a lock/cancellation check shared by request_cancel and each mutation boundary, without holding that lock during blocking handle waits.
- [x] Add independent prepare timeout test: field started but never reaches Start -> request_stop and drain; it must not use the measurement endpoint-timeout exemption. Pure read-only and mode-only failure -> zero Stop. Every submitted handle is drained or its unresolved state remains explicitly reported; no success when client future is done but SDK work is still draining.
- [x] Implement standalone `run()` cleanup and result envelope. Completed standalone preparation keeps controller connected and does not detach. Cancellation distinguishes mode uncertainty from field cleanup. Run new engine tests plus controller suite.

## Task 4: Early automatic preparation in continuous MCD

**Files:** Modify continuous class in `app/engine/mcd2100_worker.py`; extend workflow and batch tests.

**Consumes:** shared coordinator and controller preflight API. **Produces:** automatic preparation, truthful metadata and correctly scoped cleanup without changing legacy discrete workflow.

- [x] Add failing event-order tests covering Persistent startup, Driven/inactive startup, invalid magnet telemetry before any temperature/configure/gate/prepare/move/acquire call, and cancelled precheck issuing no Stop. Use a shared event list across fake optical/controller services to assert cross-device order.

```python
self.assertEqual(result['status'], 'FAILED')
self.assertFalse(any(name.startswith(('temperature.configure', 'optical.', 'gate.'))
                     for name in all_events))
self.assertNotIn('stop', all_events)
```

- [x] In `run`, invoke coordinator before `_stabilize_sample_temperature` and `_apply_setup`. Keep reference for cancellation, propagate field ownership back to `_magnet_command_issued` even on exceptions, and record preparation start/completion/last snapshot/mode-request/recovery metadata. `request_cancel` delegates to active coordinator until it exits. No optical cleanup call before optics were entered if cleanup may affect hardware.
- [x] After temperature stabilization, do a fresh read-only preflight and strict existing Driven/active checks before optics/gates. Do not re-request mode automatically if readiness has been lost. Preserve first-leg Start verification (fresh readings), reverse endpoint snapshot reuse, output integrity and normal completed detach. Update misleading comment in `_apply_setup` claiming optics precedes all magnet mutation.
- [x] Replace selective prefield failure exemptions with a general no-owned-field-command cleanup rule. Mode-only preparation failure must not trigger Stop. Existing MCD endpoint timeout behavior after acquisition remains exactly as before; preparation timeout is a different exception.
- [x] Add tests: successful prep before temp/setup; readback loss after long temperature wait prevents optics; mode-only cancel has recovery metadata/no Stop; field positioning cancel stops; ordinary acquisition cancellation still stops; successful MCD still detaches once without Stop. Update fake snapshot scripts to account for extra reads explicitly. Do not add fallback paths that skip preparation because fakes lack APIs.
- [x] Run workflow and batch suites, then combined adapter/controller/coordinator suites.

## Task 5: Native Prepare action, delayed optical readiness and shared busy lifecycle

**Files:** Modify `ui/mcd2100_panel.py`; extend panel tests; inspect existing MainWindow busy/close tests and modify only if needed.

**Interfaces:** panel `prepare_magnet()` slot; button attribute `prepare_magnet_btn`; standalone worker factory injection optional for tests; `_Runner` retained. Use operation marker to route standalone terminal handling separately. Add a small `_launch_worker(worker, operation)` helper if it avoids duplicating existing thread wiring, without unrelated panel refactoring.

- [x] Add failing tests: connected panel can Prepare with missing LF/SMU/output/sample ID; starts independent worker for selected Start field; duplicate Start/Prepare/apply-temperature/disconnect calls rejected while busy; run_state_changed True emitted before worker begins, False only after drain and no unresolved recovery.
- [x] Add native `Prepare magnet` button and automatic-preparation tooltip on Start. Implement read-only numeric input validation and controller busy/recovery guards. Recovery permits this button after drain and blocks Start/temperature/disconnect; guard slots in addition to button states. Both modes reuse existing phase/log/error displays and Stop cancellation.
- [x] Move LF `ensure_ready()` behind magnet preparation. Prefer an explicit `ensure_ready` method on `_LightFieldRotationService`, invoked by worker immediately before `_apply_setup` after preparation/temperature safety checks. For injected optical factories, use lazy construction after preparation if factory might have side effects; default service construction is inert. Preserve existing injected worker tests through an explicit supported factory seam, not early LF calls. Do not move SDK calls into GUI callback handlers.
- [x] Add terminal routing: standalone success shows `Magnet ready at Start`, updates telemetry, keeps connection, creates no experiment output and does not set `_detached_after_completion`. Standalone failure/cancel shows error and recovery message. Leave normal measurement terminal metadata/file/detach handling unchanged.
- [x] During any worker disable mutable settings and conflicting operations; re-enable according to connected/external-busy/pending/recovery state. Account for manual temperature application already in progress; preparation cannot start until it finishes. Suspend redundant telemetry/temperature polling during the owner operation as existing patterns allow; resume after terminal drain.
- [x] Integrate shutdown and shared lock: unresolved mode or pending owner work -> panel shutdown false; no forced thread termination. Keep MainWindow shared workflows locked during unresolved mode recovery even if runner exits. Let this panel's recovery Prepare run despite that internally owned lock. Clear lock on recovery success. Ensure manual refresh remains usable after drain.

```python
panel._on_terminal({'operation': 'magnet_preparation', 'status': 'COMPLETED',
                    'snapshot': safe_snapshot, 'recovery_required': False})
self.assertTrue(panel._connected)
self.assertFalse(panel._detached_after_completion)
# Recovery-needed terminal: Start disabled, Prepare enabled after drain,
# disconnect disabled, panel.shutdown(1) is False.
```

- [x] Run offscreen panel tests and existing MainWindow interlock tests. Verify keyboard/click access, button text, error visibility and no detached status for preparation. No screenshots of real hardware or live app interactions required; Qt widget tests validate this small native change.

## Task 6: Documentation, integration verification and Astra handoff

**Files:** Update `README.md` with concise MCD 2100 operation/recovery notes; all changed tests and plan checkboxes.

- [ ] Document automatic/independent preparation, defaults and configuration keys, observed-readiness policy, unknown firmware sequence, no physical validation performed, cancellation continuing firmware work, and recovery/shutdown behavior. Explain getLeadsHot without presenting it as an alarm.
- [ ] Run focused tests with offscreen Qt, using available Python runtime:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
python -m pytest tests/test_attodry2100_adapter.py tests/test_attodry2100_controller.py tests/test_magnet_preparation.py tests/test_mcd2100_workflow.py tests/test_mcd2100_batch.py tests/test_mcd2100_panel.py -q
```

- [ ] Run `python -m compileall -q app/devices/attodry2100_adapter.py controllers/attodry2100_controller.py app/engine/magnet_preparation.py app/engine/mcd2100_worker.py ui/mcd2100_panel.py utils/config.py` and `git diff --check`. Run existing commissioning unit tests (fake hardware) and relevant MainWindow tests once; do not execute scripts in `scripts/` or actual commissioning notebooks.
- [ ] Inspect `git diff --stat` and baseline temperature diff to ensure 1.67 K preservation. Record concrete test results and remaining limitations. Do not claim physical Driven switching validated by simulation.
- [ ] Hand back to root for Astra review, listing files, test commands/results, cancellation/recovery semantics and deviations. Do not commit. Astra reviews SDK sequencing assumptions, all mutation/cancel races, watcher/drain behavior, precheck ordering, recovery ownership and normal scan regression. Address its findings with focused regression tests before final delivery.

## Plan self-review

Spec coverage maps to Tasks 1-2 (safety/readiness/cancellation/owner recovery), Task 3 (shared positioning/standalone engine), Task 4 (automatic scan order/cleanup), Task 5 (UI/interlocks/shutdown), Task 6 (documentation/verification). No physical-call sequence is claimed validated. Method names in downstream tasks match the interface declarations above. Fine-grained implementation details may be adjusted to existing code after failing tests expose them; document material deviations, especially any less strict safety or recovery behavior.
