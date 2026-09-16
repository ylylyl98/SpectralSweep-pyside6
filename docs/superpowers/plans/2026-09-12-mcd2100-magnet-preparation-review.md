# Astra review record

Scope: MCD 2100 magnet preparation, implemented by Luna from the accompanying Astra plan. Review compares the working tree against `e7e6b49fc4b6c20dfdc89f09f70363307d9b9715`; the earlier 1.67 K fix is preserved. No hardware operations are part of review.

## Adapter/controller checkpoint

Astra reproduced these issues using in-memory fake adapters. Findings were sent to Luna for fixes and regression tests; resolution must be verified before delivery.

1. **P1 — Preparation can suppress field Stop.** Gate an existing SETPOINT before its mutation, queue preparation, then time out SETPOINT. The preparation branch of `request_stop` returns before setting the field cancellation event or scheduling Stop, allowing the old field mutation to proceed. Reject conflicting preparation admission and preserve cleanup for prior field work.
2. **P1 — Cached UI state allows a write after cancellation.** While owner preparation is running but the UI state signal is not delivered, submit SETPOINT, cancel read-only preparation, and release the gate. Synchronous request ownership and owner-side guards must prevent the queued write.
3. **P2 — Old watchdog cancels a new operation.** Cancel queued preparation P1, start P2, then deliver P1's old watchdog callback. Cancellation must remain scoped to the corresponding live request; terminal timers must be canceled.

Two adapter issues were corrected during the checkpoint review: Persistent startup no longer needs Driven auxiliary readiness before requesting a switch, and already-Driven startup retains compatibility with optional auxiliary telemetry.

## Complete implementation: initial review

Initial verdict: **changes required**. Astra identified these remaining issues and root assigned them to Luna for correction:

1. P1: preparation admission/Stop handling still allowed a queued preparation to swallow cleanup after an existing field SDK mutation had begun. The original regression test gated before mutation and asserted the wrong cleanup outcome.
2. P1: cancellation before preparation or during the switching phase callback could still submit preparation because mode submission was outside the cancellation lock.
3. P1: unresolved recovery released the MainWindow shared-instrument interlock on runner completion, and temperature Apply remained enabled.
4. P2: rejecting all ACTIVE preparation prevented Prepare-success followed by Start MCD; active read-only verification must work.
5. P2: a stale GUI PREPARING state could reject the next preflight even after the owner request successfully drained.
6. P2: full MCD did not propagate the configured mode wait budget independently of the ordinary operation/positioning timeout.
7. P2: preparation failure metadata could remain RUNNING, while cancellation/cleanup reporting concealed a failed Stop.

Root also required removal of test-only capability bypasses in production, actual workflow/panel preparation coverage, and prevention of lazy optical factory construction during early-failure cleanup.

## Final re-review: passed

After correction rounds, Astra independently replayed the queued-controller/live-QThread scenarios. The shared lock stayed held while the worker was live and released after an actual delayed owner request drained. The controller emits `work_status_changed` after terminal bookkeeping; the panel requires no worker/thread, no pending work, and no unresolved recovery before unlocking. No new hardware mutations were introduced by this notification.

Atomic admission, active Driven optional-telemetry compatibility, configured timeout propagation, cancellation boundaries, preparation metadata and combined primary/cleanup errors were also verified. No blocking findings remain within the reviewed scope.

Final verification on 2026-09-13:

- Root: 206 tests across adapter, controller, preparation, workflow, batch, panel, configuration, MainWindow, LightField readiness and fake commissioning suites passed.
- Astra: 72 controller and panel tests passed independently, plus the real-thread targeted reproductions.
- Python compileall and git diff --check passed.
- Offscreen fake-panel layout inspected; no physical hardware operation or commissioning performed.

Next work proceeds separately: read-only ramp-table feature, then the user-provided telemetry refresh plan. Existing changes remain uncommitted on `codex/mcd2100-magnet-preparation`.
