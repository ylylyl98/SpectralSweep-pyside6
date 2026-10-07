# WinSpec optional Start acceleration implementation plan

> **For agentic workers:** Use executing-plans to implement this approved plan task by task. Independent code review uses requesting-code-review; no parallel implementation is required.

**Goal:** Expose the verified first-body omission through normal guarded capture as an optional, default-off mode, with bounded session lifetime and safe full-Start fallback.

**Architecture:** Keep acquisition, thermal checks, transfer receipt, and spectrum normalization in the existing bridge/client. A bridge-owned manager wraps Start and owns the same validated debugger transport. A full traced frame establishes a baseline; subsequent matching frames use three endpoint breakpoints. Explicit boundaries, rollover, parameter invalidation and uncertain-owner retention remain mandatory.

**Tech stack:** XP Python 2.7/pywin32/x86 debugger; host Python 3/PySide6 and the existing WinSpec TCP protocol.

**Spec:** User-approved next-step design in this conversation; prior evidence and limitations at `docs/winspec-side-port.md`, “Resident lightweight Start validation”.

## Global constraints

- Default disabled. Existing clients and ordinary acquisition continue to work.
- Second program remains complete. No code patch, target-memory replay, changed cooling or removed thermal/receipt checks.
- Only frozen WinSpec/PVCAM/controller/PIPP/PIDC binaries qualify. Full 512×1 Free Run, sequential1, total configured exposure at most one second qualifies for initial integration; unsupported settings use normal capture.
- Exactly one same-process baseline. Settings changes, reconnect/session changes and Stop invalidate it. A gate mismatch may complete a normal frame; trace/ownership errors stop the run. Never retry a Start with an unknown outcome.
- Keep a 30-frame limit and 90-second idle debugger deadline; proactively rotate at 60 seconds. An already opened frame retains coverage until close or explicit verified retirement. Idle deadline exit must be verified before replacing a debugger.
- All COM references remain on their owning apartment. Unconfirmed Stop/detach/cleanup retains the request owner and prevents later acquisition/configuration writes.
- Rejected attempted captures are saved to a unique recovery SPE before closing. A pristine factory document may close without saving. Any unconfirmed save/close retains its owner and stops further mutation.
- Retain bounded successful diagnostic sessions; failed evidence is retained and blocks automatic continuation.
- Short acceptance only; do not impose an hours-long acquisition. Preserve unrelated workspace changes and historical evidence.

## Task 1: Package the verified runtime and add lifecycle policy

Files: create `tools/winspec/start_acceleration/` with the frozen trace/state modules, `transport.py`, `manager.py`, `__init__.py`; create `tests/test_winspec_start_acceleration.py`.

Interfaces: `Manager.capture(exp, settings, session_id, acquire)` returns ordinary `(metadata, raw)` plus `metadata['start_acceleration']`; `invalidate(reason)` retires an idle transport; `request_invalidate(reason)` merely records cancellation; `release(session_id)` releases a matching idle client. Transport exposes start/check/stop/open_frame/close_frame, detached certainty and baseline/full-frame results.

- [x] Add failing lifecycle tests using injected transport and acquisition boundary; preserve actual protocol/state validation tests.

```python
def test_changed_settings_require_full_start_before_reuse(manager, camera):
    first = manager.capture(camera, settings(500, 2), 'a'*32, acquire)
    second = manager.capture(camera, settings(500, 2), 'a'*32, acquire)
    changed = manager.capture(camera, settings(800, 1), 'a'*32, acquire)
    assert [x[0]['start_acceleration']['mode'] for x in (first, second, changed)] == ['baseline', 'optimized', 'baseline']
```

- [x] Package the previously verified modules without weakening their state/caller/step checks. Adapt only imports and explicit fallback analysis.
- [x] Implement lazy attachment, baseline learning, session/recipe invalidation, bounded rollover and verified detach. Retain reference snapshots in tests to reject incorrect redirects and false success.
- [x] Test fallback, unsupported settings, unknown launch/detach, expiry, no extra capture after errors, and successful-evidence retention bounds.

## Task 2: Connect the manager to guarded bridge and ownership cleanup

Files: modify `tools/winspec/camera_server.py`; promote `stop_owner_guard.py`; extend bridge/transfer tests.

Interfaces: managed ACQUIRE accepts `start_acceleration: {enabled: bool, session: str}`; status advertises `start_acceleration_version: 1`. `RELEASE_START_ACCELERATION` is idle-only. Frame metadata says disabled/baseline/optimized/fallback and why.

- [x] Add failing dispatch tests for absent/default-off/unsupported opt-in, settings invalidation and release ownership.
- [x] Route opt-in through manager around the real `acquire`; reject pending transfer/recovery before preparing another frame.
- [x] Use confirmed Stop, retain documents and owner on their apartment, block configuration restoration after an uncertain Stop, and release only after confirmed cleanup.
- [x] Test concurrent Stop invalidation, missing receipt, uncertain detach/Stop, no further settings writes and retention.

```python
def test_uncertain_stop_blocks_next_capture_and_settings(bridge):
    bridge.inject_stop_failure()
    bridge.reject_current_capture()
    assert bridge.recovery_required()
    with pytest.raises(RuntimeError):
        bridge.execute('SET_SETTINGS', {'exposure_ms': 800})
```

## Task 3: Expose the opt-in and forward evidence to the UI

Files: modify `utils/config.py`, `ui/instrument_panel.py`, `controllers/lf6_controller.py`, `app/devices/winspec_adapter.py`; extend adapter/UI tests.

- [x] Add `winspec_start_acceleration: bool = False`; an existing-style checkbox under WinSpec options applies on connection and is unavailable for PVCAM.
- [x] Give each WinSpecSetup a new session UUID. Send opt-in only to a bridge advertising support; explicit opt-in with an old bridge reports a clear upgrade error.
- [x] Release on disconnect; propagate acceleration and host/client timing in frame readback metadata. Do not change spectrum/count normalization.
- [x] Test default-off wire compatibility, reconnect session changes, explicit Stop, cleanup failure rejection, and unchanged average counts.

## Task 4: Package, review and run short real-path acceptance

Files: update deployment scripts/documentation; create isolated evidence under `tmp/winspec-performance-20261001/formal-start-integration-20261005/`.

- [x] Run focused host tests and independent review before hardware. Compile and run camera-free native selftests on XP using exactly frozen deployment files.
- [x] Start the new bridge on an isolated validation endpoint with exclusive ownership. Use the production host client/adapter and normal receipt protocol for off/on comparison, parameter change, Stop/restart and reconnect.
- [x] Capture actual spectrum-ready timing through the normal controller/UI signal path where the connected environment permits. Clearly distinguish any limited fixture from actual hardware/UI measurements.
- [x] Independently validate SPE/count metadata, temperature, state/redirect evidence, baseline invalidations, data receipt/cleanup, restored settings and safe server/debugger exit. Copy/hash source and evidence.

## Task 5: Deliver and document

- [x] Resolve independent review findings, run final focused checks, document exact verified scenarios and unsupported cases.
- [x] Leave the feature default off; report the measured end-to-end outcome and how to enable it. Do not commit unrelated work or claim unattended/long-duration reliability.

## Accepted evidence

Frozen v3: 26 delivered frames and one confirmed native Stop rejection in 50.829 s. Continuous10, restart, parameter-change fallback/baseline/optimized and reconnect passed. Core194 passed/4 skipped; UI19 passed. XP installation matched19 frozen source files and compiled18 Python files; prior files backed up, service left stopped. See docs/winspec-side-port.md and tmp/winspec-performance-20261001/formal-start-integration-20261005/analysis-v3.json.
