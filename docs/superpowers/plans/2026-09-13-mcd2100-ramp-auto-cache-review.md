# Automatic ramp cache — concentrated Astra review

Date: 2026-09-13. Status: PASS. All five blocking roots closed by concentrated repairs and targeted re-review.

Scope: changes against the final accepted telemetry implementation. Independent panel/controller/adapter run: 145/145 passing. Twelve additional real-controller/offscreen/gated or lifecycle probes exposed ten failures, grouped into five roots. No real App launch, hardware, computer-use or commits.

## Blocking findings

1. P1 — Active result publication lacks accepted-generation and shutdown invalidation. Old reports can be relabeled with the new connection generation; shutdown-drained reports can still publish or open a dialog. Retain drain ownership but suppress invalidated publication and follow-on work.
2. P1 — Shared admission omits live worker thread and full panel/controller display-cycle phase. Work-status notifications can insert ramp reads between magnet and temperature. Full manual telemetry completion and external-busy release do not resume pending arrangements. Use one complete idle-boundary predicate and local eligibility notifications.
3. P1 — Attempt is consumed before controller admission. An immediate atomic rejection can permanently lose the generation's automatic arrangement although no ramp read was accepted. Distinguish rejected admission from accepted device failure without timing-dependent future-state guesses.
4. P2 — Historical/failed cache validity is not consistently visible. Disconnect, new generation, latest failed refresh and synchronous failure must mark previous data historical and retain the latest cause without relabeling old timestamps.
5. P2 — Manual Refresh still opens a dialog on completion. Both automatic and manual reads must only cache/update status; View is the sole dialog-opening action.

Optional observation: silently defer an automatic busy arrangement instead of repeatedly overwriting status with generic errors. This can be handled within the shared admission correction, not as an additional feature.

## Already verified

- Actual TIMED_OUT_DRAINING retained interlock until owner drain, then telemetry resumed; one ramp read, one owner and no concurrent calls.
- Per-RPC DEBUG success/error records contain no SDK arguments or exception-content sentinel. The prior telemetry logging omission is closed.
- Existing four getters and default `(index, channel)` order unchanged; no new device mutation path.
- Automatic completion does not open a dialog; View itself makes no calls; duplicate connected notifications are deduplicated.

Regression material: `%TEMP%/astra_auto_cache_review.py`. Its twelve tests assert required behavior and should all pass after repair. Fix all five roots together, self-check, then targeted Astra re-review of these roots and their affected scope. The execution report's test command must include controller to match the reported 145-test run. No unrelated all-project testing is needed.

## Final acceptance

Luna repaired the five roots against the executable checklist and completed concentrated self-check. The original twelve probes passed after the reviewer removed an obsolete dialog cleanup from the probe itself. Targeted source review found two omissions in the same shutdown/history roots, which were corrected together.

Final independent Astra re-review: six affected acceptance checks passed, including late connected/cycle suppression after shutdown, no stale waiting-status publication before drain, historical disconnected and same-generation failed-cache display, accepted-manual consumption, and accepted fast device failure without retry. Source inspection confirmed the closing guards and metadata paths. No remaining correctness or device-safety blockers in the reviewed scope.

Latest Luna self-check: panel/controller 95/95; original review probes 12/12; targeted recheck 6/6; compileall and diff check passed. Earlier independent panel/controller/adapter run: 145/145, including per-RPC logging verification. Tests at different revisions are recorded separately, not added into a fabricated combined total.

Ready for user-operated real-device acceptance only. No physical timing, SDK rate units, index/Fast mapping or write conditions were verified. The dual-temperature preset remains deferred to documentation/SDK investigation, with no implementation in this stage.
