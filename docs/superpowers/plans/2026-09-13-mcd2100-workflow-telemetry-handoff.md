# Prepare / Start versus display telemetry — Astra repair plan

Scope: fix the user-reported Prepare and Start rejection during ordinary telemetry. One Luna implementation and concentrated self-check, then focused Astra review. No instrument operations, real App launch, computer-use, new connection, SDK write changes, sweep-rate changes or dual-temperature preset work.

User clarified timing sensitivity: do not add telemetry, temperature or safety RPCs inside the continuous scan loop. Root compared the AST of `MCD2100Worker._leg()` with HEAD and confirmed it is unchanged. Preserve this method and worker sampling. It uses field-only reads during the sweep and full snapshots at endpoint confirmation; its pre-existing optional sample-temperature getter before/after each spectrum runs only when `temperature_control_enabled` is true. This repair neither enables that option nor adds queries. Keep worker/adapter sampling code unchanged; operate at panel/controller handoff boundaries.

## Evidence and cause

User: `02:19:53 ERROR: attoDRY2100 owner work is still draining` on Prepare, and same error on Start. Earlier automatic ramp read had already logged completion. This message alone does not prove a stuck ramp read.

Astra probe `%TEMP%/mcd2100_prepare_start_handoff_probe.py` uses the real controller and EventGatedAdapter with an offscreen panel. For background and monitor requests: controller remains IDLE, request is RUNNING without timeout, both buttons remain enabled, both entry points reject with the reported text. After normal drain no worker starts; polling remains enabled and the next cycle recreates the conflict. Submission emits no work-status notification. Existing `set_polling_enabled(False)` preserves the in-flight getter and stops the remaining background cycle, so no Stop, future cancellation or reconnection is needed.

Root causes: panel rejects all pending requests before `_launch_worker` pauses polling; UI enabling and entry checks disagree; display admission lacks a status refresh; `pending and not any(display_slots)` misclassifies mixed display/control work.

## 1. Controller ownership observation

Add a narrow immutable pending-work snapshot under the controller lock. It classifies each undrained accepted request using request identity and actual display-slot ownership, command, generation and drain future. A display request must be the matching slot request and READ/READ_TEMPERATURE; a fresh safety READ or any mutation/Stop/shutdown/ramp request is not a display subscriber. Do not classify solely from a mutable source label or the existence of any slot.

Use actual owner drain state, not client future completion. Account for a terminal-state request whose drain has not yet completed. Do not change existing safety read semantics or all controller state behavior to implement this observation.

If emitting accepted work status to fix stale buttons, emit only after complete registration, slot assignment and mailbox enqueue, outside locks. Never emit from the middle of `_new_request`, because Qt slots may run synchronously and reenter admission.

## 2. Unified validated workflow intent

Prepare and Start retain all current connection, workflow, recovery, input, SMU/optical and parameter validation. Remove the blanket rejection of display-only work. Reject incompatible real control work, including mixtures with display reads. Start still rejects unresolved recovery; Prepare may perform its existing fresh recovery check.

Build the existing worker from validated parameters before deferring, preserving lazy optical creation. Real constructors capture/validate data but do not call the SDK. Capture the accepted generation, operation, worker, unique token, metadata run and prior display-monitor/polling state in a single intent. Do not reread edited UI values after waiting.

```python
def request_handoff(worker, operation, metadata_run):
    validate_no_active_worker_or_thread_or_intent()
    validate_not_closing_disconnecting_external_busy_or_incompatible_control()
    intent = PendingIntent(token(), controller.generation, worker,
                           operation, metadata_run, previous_display_state)
    self.pending_intent = intent  # before reentrant shared-lock signals
    hold_shared_workflow_interlock()
    pause_background_and_all_new_panel_display_sources()
    show_waiting_with_cancel()
    advance(intent.token)

def advance(token):
    if token_is_stale(token):
        return
    revalidate_lifecycle_generation_external_busy_control_and_recovery()
    if any_original_display_request_not_really_drained():
        show_normal_wait_or_timeout_awaiting_drain()
        schedule_tokenized_local_Qt_check()
        return
    # No new display source can enqueue from this panel now.
    consume_intent_once_while_retaining_interlock_and_display_pause()
    launch_original_worker_with_original_validated_arguments()
```

Use both work-status notifications and bounded tokenized Qt checks; do not busy-wait or sleep the UI. Both manual display groups must drain. A client watchdog timeout is not permission to start. The worker then performs its original fresh preflight/preparation/safety reads; display cache is never safety evidence.

The UI intent/shared MainWindow lock coordinates application entry points; do not claim it is a global atomic reservation against arbitrary external threads directly invoking controller APIs. Any incompatible controller work discovered before launch must prevent launch. Do not add a broad controller reservation framework without a concrete necessity/conflict report.

## 3. Suppress display competition and keep measurement reads

All new display request paths must recognize pending intent: manual refresh, background polling, temperature display monitor, automatic/manual ramp refresh, and callback continuations that can submit another group. Already accepted requests finish naturally; do not cancel shared futures. Cache-only View remains local.

During actual MCD/Prepare work, preserve the existing display pause. Worker field sampling, fresh safety/status checks and necessary temperature checks continue unchanged. Display values may update from worker events. Only after worker and cleanup/owner drain finish should the prior eligible display scheduling resume.

Buttons and entry checks must agree: Prepare/Start may accept a display-only handoff, duplicate clicks do nothing, waiting blocks competing mutations and enables cancellation. Shared interlock remains held while intent exists, including when no worker thread has started. Guard `_release_interlock_if_drained` against premature release.

## 4. Cancel, lifecycle invalidation and errors

Cancel waiting invalidates the token and removes the unstarted intent. Do not run or cancel the worker to simulate cleanup; no SDK Stop, mode action, close or future cancellation. Finalize metadata locally (`metadata_run.cancel(reason)`; `fail(error)` for errors). Start currently creates metadata before worker construction; never leave that run as running when the intent is abandoned.

Closing, disconnect/generation changes, external ownership loss, a newly conflicting control request or construction/launch failure must abort safely and invalidate callbacks. Start recovery rejection and Prepare recovery handling remain distinct. Restore only eligible prior display state and never rearm after shutdown/disconnect. If actual owner work is still draining, retain the existing shared interlock until drain before releasing; intent cancellation does not mean the device thread became idle.

Ensure the synchronous MainWindow `run_state_changed(True)` callback cannot race ahead of intent installation and launch auto-ramp. On failure after worker construction, metadata and references must be cleaned without invoking instrument cleanup for a worker that never ran.

## 5. Concentrated tests before one handoff

- [x] Prepare/Start with background, monitor and manual one/two-group pending: waiting, all owner reads drain, then exactly one worker launch with original validated arguments.
- [x] Duplicate click and simultaneous timer/terminal callback: one launch. UI edits while waiting do not alter the captured worker.
- [x] Waiting and measurement: zero extra independent display/monitor/ramp reads; already accepted getters not cancelled; fresh worker safety reads remain separate.
- [x] Cancel waiting, shutdown, generation/disconnect, external busy: no worker launch/Stop, stale callbacks inert, metadata finalized, interlock retained through any remaining drain.
- [x] Client timeout before actual drain: wait; start only after real drain, with original fresh preflight.
- [x] Pure control and mixed display/control pending, including ramp, temperature/field mutation, Stop/shutdown: reject safely, never misclassify as display-only.
- [x] Start recovery rejected; Prepare fresh recovery check still available. No cache used to clear recovery.
- [x] Validation/construction/launch failure and idle immediate-start behavior retain correct polling/interlock/metadata handling.
- [x] Run affected panel/controller/telemetry and existing preparation/workflow tests as justified, compile changed modules and diff-check. No unrelated full-project tests.

Add real-controller/gated-fake/offscreen regression cases rather than replacing the admission path with mock state. The initial probe documents the old failure; update/invert its defect assertions for fixed behavior, do not count the original defect reproduction as acceptance.

## Handoff / review

Luna reads this plan against current code, preserves existing fixes, and reports any design/code conflicts together with a proposed alternative before pursuing a contradictory implementation. Finish all changes and relevant self-check together. Handoff exact commands/results, changed functions and known limitations. Astra gives one concentrated list of any remaining blockers with executable corrections, then rechecks only the fixes and affected paths. No optional optimization beyond this repair.

## Implementation checkpoint

The controller now exposes an immutable `pending_work_snapshot()` classified under its request lock by request identity, display-slot ownership and actual owner drain state. The panel uses a tokenized workflow intent for Prepare and Start: it validates and constructs the existing worker before waiting, pauses new display sources, waits for all accepted display owners to drain, and then launches exactly once. Mixed control/display ownership remains rejected.

Waiting cancellation finalizes metadata without running or cancelling the worker and without issuing a field Stop; the shared interlock remains held until any accepted display request drains. Worker sampling paths are unchanged.

Focused validation: panel/controller suites **107 passed**; preparation, workflow, batch and telemetry suites **68 passed**; compileall and `git diff --check` passed. The original handoff probe is a defect reproduction with obsolete assertions; its optimized run now shows empty Prepare/Start errors, polling paused during handoff, and safety calls beginning only after the gated display owner drains.

Restoration follow-up: saved polling/monitor state now carries the controller generation and is discarded on generation change, disconnect, or shutdown. Waiting cancellation no longer enables polling before accepted display work drains, and terminal-worker fallback polling is used only when no saved state existed, including synchronous interlock-release re-entry. Focused restoration tests (9 panel cases) and the corrected handoff probes pass; `py_compile` and `git diff --check` pass.
