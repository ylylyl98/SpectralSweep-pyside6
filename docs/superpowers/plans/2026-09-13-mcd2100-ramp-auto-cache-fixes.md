# Automatic ramp cache — Astra executable repair checklist

User workflow: one concentrated repair and self-check by the existing Luna writer, then targeted Astra review of these changes and their effects. Preserve repairs already made; reconcile this checklist with current source before editing. If a proposed mechanism conflicts with controller contracts, report the conflicts together with a concrete alternative. Do not implement a contradictory recipe or hand off one issue at a time.

Evidence: independent panel/controller/adapter 145/145 passed, while `%TEMP%/astra_auto_cache_review.py` had 10 failures out of 12 required-behavior probes. The twelve probes already assert correct behavior. No hardware is needed. References below use function names because line numbers change during repair.

## 1. Separate active owner lifetime from permission to publish

**Evidence / root cause:** `test_stale_active` relabels an old report with generation 2; real-controller `test_real_shutdown_suppresses_completed_publication` publishes an interrupted report after shutdown. `_poll_ramp_tables` validates only handle identity; `_store_ramp_tables_report` reads the controller's generation at completion. Shutdown invalidates pending scheduling but not active publication.

**Modify:** panel `_start_ramp_tables_read`, `_poll_ramp_tables`, `_store_ramp_tables_report`, `_on_disconnected`, `_on_connected`, `shutdown`. At acceptance capture immutable handle identity, accepted generation, current lifecycle/shutdown token, source and start time. Pass/capture this context in every polling callback; do not replace it with current panel fields after reconnect. Reuse the actual connection generation. A panel lifecycle token only invalidates local callbacks; it is not another connection counter.

```python
valid = (context is active_context and connected and not closing
         and context.generation == controller.generation
         and context.token == current_panel_token)
if not handle.drained_done:
    if valid:
        update_waiting_status_if_needed()
    schedule_same_handle_drain_check()  # no new device request
    return

# Drain bookkeeping is mandatory even when publication has been invalidated.
if context is not active_context:
    return
clear_this_active_context_and_handle()
if not valid:
    release_interlock_only_if_all_owner_and_worker_work_drained()
    return  # no cache/status/dialog updates, no auto retry or polling restart
publish_result_with(context.generation, original_read_time, elapsed)
```

Disconnected/shutdown handlers cancel not-yet-started arrangements and invalidate publication, but must not clear an undrained active handle or release its interlock. Shutdown's existing cooperative cancellation may finish with a partial result; that does not authorize UI publication after shutdown. A failed shutdown because drain is pending must remain publication-invalidated until its lifecycle is explicitly resumed; no silent rearming. Ensure generation checks cover current callbacks as well as ordinary and late-drain result paths.

**Impact:** local ramp lifecycle/cache only; preserve controller cancellation, owner drain and shared workflow lock. Tests must also prove no follow-on getter or dialog after invalidation.

## 2. One complete idle-boundary predicate and deferred-entry function

**Evidence / root cause:** `test_real_cycle_gap` observes `connect → magnet → temperature → magnet → ramp`, inserting ramp before the second temperature group. `test_thread_defers`, `test_manual_full_cycle_marks_ready`, `test_external_release_retries` expose the other missing conditions. `has_pending_work` can become false between groups while a display cycle is still active.

**Modify:** one panel eligibility helper used by both `_start_ramp_tables_read(auto=...)` and a local `_try_pending_ramp_read` (names may match existing code). It must require connected/current generation, no closing/disconnect transition, no active ramp, no worker or running thread, no external busy, no temperature apply, no mode recovery, no controller pending owner work, **no panel manual telemetry cycle and no controller background display cycle**. Auto additionally requires pending, unconsumed generation and completed initial full state-query cycle. Manual need not wait for the auto flag, but uses the same idle predicate.

Use the controller's existing cycle state or a minimal read-only property; do not create another scheduler or stop the existing cycle mid-group. Existing controller `display_cycle_finished` is emitted after both groups drain and `_display_poll_cycle` is cleared. Manual `_poll_telemetry` must similarly mark ready and attempt pending work only after both handles drain and `_telemetry_cycle` is cleared.

```python
def on_full_cycle_finished(generation):
    if not current_connected_generation(generation) or closing:
        return
    initial_queries_drained = True
    try_pending_read()  # the same predicate checks both display consumers

def on_work_status_or_external_release_or_worker_finish():
    try_pending_read()  # a local check; no speculative ramp submission
```

Call the local retry on `set_externally_busy(False)`, `work_status_changed`, and actual worker/thread finish. While either cycle remains active, keep pending silently. At a full-cycle gap try pending **before** starting the next background cycle; do not repeatedly disable/re-enable polling or introduce catch-up reads. Manual and auto subscribers may overlap, so completion of one does not imply the other cycle is clear.

**Impact:** only ramp admission and observation hooks. Preserve complete telemetry ordering, existing safety reads and Start/Prepare/Stop ownership. Add an event-trace assertion that each eligible full cycle ends before ramp submission and pending eventually starts once.

## 3. Consume the arrangement only after atomic acceptance

**Evidence / root cause:** `test_real_atomic_rejection_not_consumed` inserts ordinary work immediately before the real controller reader; its rejected handle is immediately FAILED, no ramp SDK call occurs, but the arrangement was already consumed. Future state is insufficient: an accepted device failure may also complete immediately.

**Modify:** expose a minimal immutable admission fact on `OperationHandle` (the in-progress `accepted` property is suitable). It is true for actually admitted owner requests and false for `_immediate_failure` admission rejection. Retain it after requests finish or leave `_requests`; never infer acceptance from current registry membership or `future.done()` alone. Ensure all immediate failure paths return false before the caller receives the handle. Preserve existing request/queue lock order.

```python
handle = controller.read_ramp_tables_async()
if not handle.accepted:
    clear_local_unaccepted_handle()
    release_only_if_drained()
    # pending unchanged; no device read was attempted
    # report manual rejection once; auto waits for the next eligibility event
    return

capture_active_context(handle.generation, ...)
if pending_generation == handle.generation:
    attempted = True
    pending = False  # applies to BOTH auto and accepted manual refresh
poll_until_actual_drain(handle)
```

Tests/fakes must expose acceptance accurately; production must not assume an unknown handle is accepted. Busy/state admission rejection keeps pending and awaits a future eligibility event rather than immediately recursing. An **accepted** full/partial device failure or timeout consumes the attempt and must not automatically retry. Unsupported API or a persistent unexpected synchronous exception should show a terminal arrangement error once, not cause an event-driven error loop; distinguish these from known transient admission rejection. Make this exception policy explicit in code/tests.

**Impact:** one additive handle fact plus ramp submission bookkeeping; no queue rewrite or new SDK operation. Add accepted-fast-failure versus rejected-admission tests, accepted manual consumes pending, and retry-after-busy exactly once.

## 4. Make cache origin, latest outcome and historical validity visible

**Evidence / root cause:** `test_disconnected_cache_validity` and `test_failed_same_generation_history` show that `_ramp_tables_cache_valid` is written but not consistently used. Old successful reports can appear current after a newer failure or disconnect. Synchronous errors skip invalidation.

**Modify:** keep cached report/generation/read time separate from latest attempt outcome/error. Derive View's historical flag from not connected, generation mismatch, or invalidated latest result. A new generation and disconnect invalidate existing cache without clearing its data or time. A latest refresh that returns no usable report invalidates the older report, preserves its original metadata, and presents the latest error. Use the same error helper on synchronous failures and client/drain failures. Partial reports from the **current accepted attempt** replace the cached report with their own generation/time and explicitly partial/error outcome; they are current partial observations, not a fabricated success.

On disconnect set the visible status to previous connection/stale. On latest failure show its cause and that View contains a historical report if one exists. Include historical/partial/latest-error context in the dialog heading or metadata label, not just an internal boolean. Do not let a success-formatted old tooltip overwrite the new failure. Opening View must not alter cache validity, generation, time or latest outcome.

**Impact:** panel cache/status/dialog metadata; raw table cells and getter parsing unchanged. Verify same-generation failed refresh, synchronous reader error, new-generation failure, partial results and disconnect retain old raw values/time while showing the correct origin/outcome.

## 5. View is the only dialog-opening action

**Evidence / root cause:** `test_manual_opens_only_via_view` fails because completion retains `if not auto: _show_ramp_tables_report(report)`; the old overlap test also asserts this obsolete behavior.

**Modify directly:** remove completion-driven `_show_ramp_tables_report` calls for both auto and manual. Only `view_ramp_tables()` opens/updates a dialog. Keep existing button attribute and `read_ramp_tables()` compatibility. Repair tests to assert Refresh completion is dialog-free, then explicitly invoke View and check both raw tables and zero additional communication. Do not silently keep an outdated test expectation by changing the approved behavior.

**Impact:** ramp dialog opening only; no changes to unrelated dialogs.

## Concentrated self-check and handoff

- [x] Reconcile and complete all five fixes and affected regression tests before handing off.
- [x] Run `%TEMP%/astra_auto_cache_review.py`: all twelve required-behavior probes pass. They are already positive acceptance assertions.
- [x] Run panel/controller regression modules together, including new race and lifecycle cases. Adapter logging and getter order were independently verified; do not change/retest adapter again unless a repair actually affects it.
- [x] Compile the changed Python modules and run `git diff --check`.
- [x] Correct execution report command to include controller and record actual latest counts; keep plan checkboxes consistent with evidence.
- [x] One final handoff: changed functions, resolution of each root, commands/results and any concentrated design conflicts. No optional scope, hardware, real App, computer-use or commits.

Astra re-review will inspect only this repair delta and affected lifecycle/admission/cache paths and rerun the appropriate acceptance probes. Do not reopen already verified getter safety, unrelated telemetry implementation or new temperature-control work.

## Targeted re-review: remaining omissions in items 1 and 4

The original twelve probes now pass (reviewer removed an obsolete `.close()` after asserting that no dialog exists). Six additional assertions of these same roots produced four failures and two passes. No new scope is added.

1. Initialize `_closing=False`; set True at the very beginning of shutdown, before token invalidation. Return immediately from `_on_connected`, `_on_display_cycle_finished` and `_start_ramp_tables_read` while closing. Do not enable polling or rearm from late signals. In `_poll_ramp_tables`'s `wait_drained` timeout branch, guard **status publication** with `not stale_request`; continue checking actual drain for both valid and invalid requests. Preserve the existing invalid-result cleanup and interlock handling.
2. On disconnect explicitly mark the ramp status previous-connection/stale. Pass latest error and the precise stale reason into dialog metadata: same-generation latest-refresh failure is not a previous connection. Display the concrete `_ramp_tables_last_error`, retain the original report time and show the correct historical reason in status/tooltip. Reuse this handling for synchronous and client/drain failures; current successful/partial reports have their own outcome and clear stale prior errors as appropriate.

Targeted acceptance file: `%TEMP%/astra_auto_cache_recheck.py`. Required: `test_late_connected_after_shutdown_cannot_rearm`, `test_stale_drain_wait_does_not_update_status`, `test_disconnect_status_is_historical`, `test_latest_failure_is_visible_in_history_dialog`. The other two checks validate accepted-manual consumption and accepted fast-failure no-retry. Run all six plus the original twelve and directly affected new regressions together before one handoff. No repeated 93-test run is required without a new impact reason.

Final status: PASS. The concentrated fixes and the two targeted omissions above are closed. Latest self-check: 95 panel/controller tests and 18 review probes; final independent Astra recheck: 6/6. See the adjacent review report for revision-specific counts.
