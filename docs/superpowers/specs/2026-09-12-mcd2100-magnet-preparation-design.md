# MCD 2100 magnet preparation design

Date: 2026-09-12. Requested workflow: Astra design and plan, Luna implementation, Astra review. The preceding conversation approved implementing automatic preparation and a separate preparation button; no additional approval loop or hardware operation is part of this task.

## Outcome

`Start MCD` performs read-only magnet safety validation first, requests Driven mode when necessary, verifies readiness, enables field control and reaches the requested Start field before temperature, optical or gate setup. `Prepare magnet` performs the same magnet-only preparation without needing LightField, a sample ID, output directory, rotator or SMU. Success leaves the instrument connected at Start. Existing continuous acquisition, reverse legs, output files and successful completed-run detach remain intact.

## Constraints

- Preserve the existing uncommitted 1.67 K temperature fix and its tests.
- Work on the existing `codex/mcd2100-magnet-preparation` branch; do not commit or operate real hardware.
- Use Python, PySide6 native widgets, the existing SDK adapter and single-owner controller. No new framework or UI restyling.
- Only `setDrivenMode(channel, True)`, existing field setpoint/start/stop operations and existing readback APIs may control the magnet. Never implement manual switch-heater, supply-current, persistent-mode rollback, ramp-rate or limit programming.
- Quench must be explicitly false. Field and magnet temperature must be finite and within the configured limits, capped by the existing hard 6 T and 7 K ceilings. Sample temperature is a separate existing control.
- Configuration and telemetry failures fail closed before further mutations. A precheck failure alone never sends a magnet Stop.
- A request acknowledgement is not physical readiness. UI messages and documentation must distinguish observed software readiness from an unverified physical transition guarantee.

## Evidence and limitations

Both `CRYO2100/magnet.py` and configured runtime `D:/Insturment control v3/CRYO2100/magnet.py` expose `setDrivenMode`, `getDrivenMode`, `getPersistentMode`, `getHState`, switch-heater state and lead field. Their generated docstrings do not specify physical completion, cancellation, sequencing with field control, or HState enumeration. Commissioning JSON records show the existing instrument ready with `IDLE`, driven=true, persistent=false, heater=true and lead field equal to magnet field. They do not show a transition into Driven.

`getLeadsHot` is documented as checking whether current runs in power-supply leads; commissioned normal Driven snapshots report true. It is informational and must not be treated as an overtemperature alarm.

After an app-issued or unresolved mode request, require three consecutive fresh safety-valid snapshots with driven=true, persistent=false, heater=true, finite lead field within 0.001 T of magnet field, and HState exactly `IDLE` (normalize whitespace/case only). This is a conservative software policy based on available readbacks and the locally observed stationary state, not a vendor-certified completion definition. Expose the lead tolerance and mode timeout as configuration settings; unknown states or missing required transition evidence cannot authorize positioning. Do not guess additional HState strings. Default mode timeout: 300 s; default sampling period: max(existing polling interval, 0.2 s).

For an already Driven, nonpersistent startup with no unresolved app transition, retain existing compatibility with optional auxiliary telemetry. Explicit contradictory heater=false or finite lead mismatch must not be silently accepted; enter read-only readiness waiting and apply the strong criterion before moving. A presently ramping HState need not be interpreted as a mode transition: the normal high-level setpoint path already supports retargeting active field control. Never loosen strict recovery based solely on newly true mode booleans.

If the firmware does not complete a mode request under this sequence, time out without issuing field-control commands in an attempt to complete it. The detailed vendor sequence remains a documented commissioning question. No real instrument test is authorized in this implementation task.

## Architecture

1. The adapter owns safety validation and SDK operations. Add a read-only preparation preflight that accepts valid Persistent telemetry and inactive field control, plus a bounded `prepare_driven_mode` operation that uses only the high-level mode request and readbacks. Preserve the stricter Driven requirement for ordinary setpoint/start operations.
2. The controller adds explicit preflight and prepare-Driven commands. All SDK calls, including readiness polling, stay on its existing owner QThread. Preparation cancellation uses a separate cooperative event; it is not the field Stop event. Long mode-readiness timeouts must not be cut short by the controller's default approximately 12 s request watchdog.
3. A small shared engine module orchestrates preflight, Driven preparation, set/start positioning and five-snapshot start-field confirmation. Both the continuous worker and a standalone preparation worker use it. Existing acquisition and endpoint logic stay in `mcd2100_worker.py`.
4. The panel reuses its `_Runner` and QThread lifecycle for standalone preparation, so `run_state_changed` continues to activate MainWindow's shared-instrument interlock. It differentiates standalone preparation completion from measurement completion; preparation must not trigger detach or experiment completion.

## State and ownership

Track mode transition uncertainty separately from whether this operation has issued field commands. Mark uncertainty immediately before the SDK mode call, since an exception or timeout may occur after hardware accepted it. Mark field ownership before submitting the first setpoint/start, preserving conservative cleanup for an in-flight field mutation.

| Point of cancellation/failure | Required action |
| --- | --- |
| Read-only precheck or readiness observation, no app mode/field command | Abort; no Stop; no optical/gate/temperature setup |
| Before queued mode command starts | Cancel that command cooperatively; do not later clear its cancellation event and execute it |
| During/after mode request, before field command | Stop scheduling work; allow in-flight SDK call to drain; do not issue field Stop, mode rollback, or close transport; retain recovery-needed state |
| During field positioning or later owned field activity | Existing idempotent field Stop and drain semantics apply |
| Mode timeout with SDK call still running | Client timeout plus independent owner drain; keep mutation controls disabled until drain; no second socket or thread termination |

Recovery allows fresh telemetry and `Prepare magnet` after owner drain. If a prior mode request is unresolved, retry must first observe the strict readiness criterion; it must not reissue the mode command blindly. While unresolved, block setpoint/start, temperature mutations, disconnect and automatic shutdown/Stop. Read-only operations remain available. Successful strict verification clears uncertainty and resumes normal controller behavior. `shutdown()` returns false while uncertainty or undrained SDK work remains; existing MainWindow close handling keeps the app open. User text explains that the device transition may continue, and that Prepare magnet rechecks it. Keep the shared workflow lock while recovery is unresolved, but permit this panel's recovery action.

Preparation success requires active field-control telemetry, verified Start setpoint (even when actual field is already at Start but the old setpoint differs), and five consecutive fresh snapshots within the existing Start gate. If field control is inactive, arm Start and start it even if actual field already equals Start. Preparation positioning timeout is a preparation failure requiring owned-field cleanup, not the existing measurement endpoint-timeout exception that intentionally leaves field control active.

## Startup ordering and UI

Validate user inputs and cached connection flags before mutation. Full magnet preflight validates both sweep endpoints using configured limits before a mode request. Move `LightField.ensure_ready()` out of the GUI's early start path and behind successful preparation; ensure no optical factory with hardware side effects runs early. The default service constructor is already inert. Add an explicit service readiness method or a lazy factory so configuration still follows initialization.

Show stages: `Checking magnet`, `Switching to Driven`, `Waiting for Driven readiness`, `Moving to Start: current → target T`, `Magnet ready`, followed by the existing temperature/setup/measurement phases. The Start button tooltip explains automatic preparation. The standalone button is `Prepare magnet` beside magnet/start controls. Reuse the existing error/log/status display and stop button; during mode cancellation say `Preparation cancelled; device mode transition may continue. Use Prepare magnet to recheck readiness.`

Disable duplicate Start/Prepare, disconnect, manual temperature apply and mutable run settings while running. Guard slots as well as disabled widgets. Standalone Prepare is independent of acquisition/output requirements. After preparation success keep connection and telemetry active. On failure keep the error visible and return controls only when owner work is drained; recovery-needed permits only telemetry and recovery preparation among device actions. Interlock other MCD/power-sweep workflows during preparation and unresolved recovery.

The continuous worker prepares at run entry, before temperature/setup mutations, then performs a fresh read-only readiness check after potentially long temperature waiting and again before acquisition as existing `_leg` checks require. If readiness was lost after setup, abort with appropriate owned-field cleanup; do not silently request another mode transition mid-experiment. Existing reverse-leg fresh endpoint reuse remains unchanged.

## Validation

Use fake SDKs, event-gated controller adapters, fake clocks, worker event traces and offscreen Qt tests. Cover readiness evidence, unknown telemetry, both field-control states, stale setpoints, initial non-Driven paths, precheck ordering, cancellation at each boundary, timeout/drain, unresolved recovery/shutdown, standalone UI independence and MainWindow busy signaling. Keep current continuous completion/detach and endpoint timing behavior covered. No test opens real instrument connections. Physical commissioning remains unperformed and must be stated in the delivery.
