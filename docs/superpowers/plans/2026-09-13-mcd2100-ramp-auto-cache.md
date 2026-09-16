# MCD2100 automatic ramp-table cache — user-approved phase 1

Date: 2026-09-13. Sequence: finish telemetry fixes and targeted Astra re-review, inspect its final interfaces, then one Luna implementation and one concentrated Astra review. Phase 1 implementation and concentrated Astra review are complete (PASS). Real-device acceptance remains user-operated and unverified.

No real App launch, computer-use, device connection or instrument calls by agents. No new connection, SDK write, mode change, field action, Stop or disconnection as a side effect of reading. Real-device acceptance is performed by the user. Phase 2 below is evidence gathering only, not authorization to implement or execute writes.

## Entry / Astra interface check

- [x] Telemetry accepted with no unresolved correctness or device-safety findings.
- [x] Inspect final telemetry delta, generation lifecycle, polling cycle completion/drain, shared ownership and existing ramp APIs. Record reusable interfaces here before assigning implementation; do not duplicate them.
- [x] Preserve all existing uncommitted preparation, 1.67 K, ramp and telemetry changes.

### Astra interface check after telemetry PASS

The controller already provides `generation`, `has_pending_work`, `work_status_changed`, `read_ramp_tables_async()`, per-request `generation`/`completed_at`/`drained_done`, and display generation/timestamp envelopes. Reuse them; do not introduce another broker, connection generation counter, owner or cache for telemetry. Existing `_poll_ramp_tables` retains partial owner results after client timeout and must keep that contract. Existing `_RampTablesDialog` already renders both raw tables.

The background scheduler currently clears its cycle and starts its one-second single-shot timer after temperature drain. Add one narrow generation-bearing cycle-finished signal at that boundary if needed; the panel can use it and manual `_poll_telemetry` completion to mark initial queries drained and attempt pending work. Emit only after both groups are done and the cycle is cleared; do not trigger ramp reads after every individual snapshot. A generation-bearing cycle-finished notification is an observation, not a device request. `work_status_changed` and external-busy release can retry local eligibility after initial queries complete. Avoid repeating timers that call the getter merely to test readiness.

Connection success reaches the panel through both the controller signal and `_poll_connect`; deduplicate `_on_connected` for the same generation before resetting state or rearming. Keep pending/attempted generation separate from cached report generation. Keep active-read identity/generation separate from pending scheduling so stale callbacks cannot clobber new state. Invalidate pending callbacks on disconnect/shutdown without prematurely forgetting an active owner-draining read or releasing its interlock.

Use a single internal start method for auto/manual: check connected/current generation, initial-query completion for auto, worker/thread, external busy, temperature operation, recovery, all controller pending work, and display cycle phase before submitting. Preserve controller atomic admission as the final authority. Consume the pending automatic arrangement when a read is actually accepted; a failed device read is still the generation's attempt. Do not spin on failed admission or hide errors. Schedule at the full-cycle idle gap to avoid continuous polling starvation; existing polling deferral and ramp guard can keep telemetry off the queue during the read.

Split cache storage from `_show_ramp_tables_report`: completion stores report/metadata and status only. A new View button is the only dialog-opening path. Preserve `read_ramp_tables_btn` and `read_ramp_tables()` compatibility while relabeling the existing action Refresh. Include wall-clock read time and monotonic elapsed duration in cache/status/logs and the viewing dialog; old reports retain their original times. Cache validity requires matching connected generation and a completed accepted report; a failed newer attempt must show its error while clearly retaining any old report as historical.

All nine user acceptance cases below need behavior tests, including at least one real controller + gated adapter/offscreen integration proving full telemetry-cycle ordering and timeout/drain recovery. Fakes must simulate duplicated connected notifications and generation changes; do not bypass the lifecycle under test. No unrelated regression expansion.

## 1. Reuse existing read flow

Primary files: `ui/mcd2100_panel.py`, `tests/test_mcd2100_panel.py`; modify `controllers/attodry2100_controller.py` and its tests only if the existing interface is insufficient. Adapter changes require corresponding adapter tests.

- [x] Automatic and manual requests use the existing `read_ramp_tables_async()` and owner queue.
- [x] Retain per-item errors, partial results, timeout-awaiting-owner-drain and all interlocks.
- [x] Add no SDK writes, connection, or unrelated refactor.

## 2. One automatic attempt per connection generation

| Condition | Behavior |
| --- | --- |
| Disconnected | No request |
| Connected, necessary initial state queries incomplete | Mark pending |
| Other device work or mode recovery required | Keep pending; defer |
| Existing read admission conditions satisfied | Start one complete Current + Default read |
| Completed, partially failed, or failed | No automatic retry this generation |
| New connection generation | Arrange one new attempt |

- [x] Repeated connected notifications for the same generation do not rearm.
- [x] Arrange the pending read in an idle gap after a regular telemetry cycle completes, without starvation from continuous polling.
- [x] Eligibility checks do not submit speculative ramp requests.
- [x] A successfully submitted manual refresh consumes a not-yet-started automatic arrangement.
- [x] Repeated clicks while a read is active do not enqueue duplicates.

## 3. Cache and UI

- [x] Cache report, generation, read time, and complete/partial failure status.
- [x] Automatic completion updates status without opening a dialog.
- [x] **View ramp tables** displays the cache only, with zero device communication.
- [x] **Refresh ramp tables** explicitly requests a new read through the shared flow.
- [x] Ordinary telemetry refresh and tab switching do not reread tables.
- [x] Disconnect retains old data for viewing, clearly marked previous connection / stale.
- [x] A failed read on a new connection cannot present an old cache as currently valid.
- [x] Old-generation callbacks cannot overwrite new data or reopen dialogs.
- [x] Display both tables, original channel/index/range/rate, timestamp and partial errors. Do not infer SDK units, index base, or Fast mapping.

## 4. Failure and shutdown

- [x] Failure or partial failure reports its cause without automatic retry; manual refresh remains available when eligible.
- [x] Timeout preserves busy state until owner drain; subsequent telemetry can resume.
- [x] Disconnect and panel shutdown invalidate not-yet-started arrangements and callbacks.
- [x] Old callbacks cannot start follow-on work, change current status or open dialogs.

## 5. Behavioral tests and unified handoff

Write behavior tests before implementation using fakes/offscreen Qt. Prefer panel/controller suites; include adapter suite only if adapter changes.

- [x] Exactly one automatic request per generation; repeated connection notification is deduplicated, reconnect rearms.
- [x] Busy defers, later idle starts once.
- [x] Automatic/manual overlap and repeated clicks do not duplicate requests.
- [x] Telemetry, View and tab switching do not add ramp calls.
- [x] Automatic completion opens no dialog; explicit View shows Current and Default tables.
- [x] Disconnect marks stale; delayed old callback cannot overwrite the new generation.
- [x] Partial errors retain successful rows; failure does not create an automatic retry loop.
- [x] Timeout keeps the interlock through drain, then telemetry resumes.
- [x] Luna provides exact changed files, commands/results and deviations in one handoff.
- [x] Astra concentrates review on lifecycle, deduplication, communication interlock and timeout correctness. Fix blocking findings and re-review only changes and their affected scope.

Finish when acceptance passes with no unresolved correctness/device-safety issues. Optional optimizations do not extend this phase.

## 6. User-operated real-device acceptance

Agents provide short instructions and analyze returned logs; the user starts App and connects an idle instrument.

1. Observe one automatic completion; record both raw tables, timestamp, elapsed time and all errors.
2. Leave telemetry running and confirm no further automatic ramp-table requests.
3. View the cache and verify no communication; Refresh and verify exactly one new complete read.
4. Check UI responsiveness during the read and telemetry recovery after drain.
5. Reconnect validation is optional at a suitable time chosen by the user; record stale cache and one new generation's attempt.

## Phase 2: conditions before any write-feature proposal

After real results are supplied, compare SDK, manuals and instrument panel evidence for index, interval boundaries and rate units; ordinary sweep versus Fast/zero-leads/current-matching behavior; allowed SDK write states; magnet rate limits; activation timing, persistence and readback verification.

Known supplied system evidence: APS100, magnet coefficient 2044.9 G/A; charging 0.0344 A/s from 0–40 A and 0.0172 A/s from 40–44.0116 A. These do not establish SDK rate units or index/Fast mapping. Historical notebook T/min labels are not authoritative. Reading successfully does not satisfy the phase-2 conditions. With insufficient evidence, deliver the reader and explicitly unresolved conditions; do not enable writes.
