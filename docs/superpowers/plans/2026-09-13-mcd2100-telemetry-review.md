# Telemetry concentrated Astra review

Date: 2026-09-13. Status: PASS. All blocking findings closed by concentrated review and targeted re-review.

Scope: telemetry delta after the accepted magnet-preparation and explicit ramp-table features. Independent Astra source review and 195 focused tests (passing), plus 11 defect reproductions and one isolated lock-order reproduction. No real application launch, instrument access, or computer-use during review.

## Blocking findings

1. P1: diagnostics drain callback introduces controller/mailbox ABBA lock inversion during queued cancellation and concurrent submission, potentially blocking Stop.
2. P2: automatic refresh and temperature monitor wait indefinitely after a watchdog timeout has actually drained because they check request state without independent drain completion.
3. P2: public display subscriber future exposes the shared owner future, allowing one subscriber to cancel all consumers.
4. P2: a drained display slot is still joined until GUI terminal processing, bypassing `max_age_s=0` and cache-age rules.
5. P2: cache/join can precede lifecycle validation; reconnect does not advance generation; controller disconnect does not stop its scheduler; panel callbacks do not enforce stored generation.
6. P2: background and monitor delivery lose original completion time, resetting apparent age on cache reuse or delayed Qt delivery.
7. P2: polling resumes only after preparation, not other connected measurement/cancellation ownership releases.
8. P2: manual refresh rejects background display pending work instead of joining; cached cycles leave the button enabled; timeout-awaiting-drain wording is absent.
9. P2: missing agreed diagnostics for cache/join disposition, manual/background/monitor source, whole-refresh duration, and DEBUG logging.

Also repair misplaced FakeController methods and unreachable existing ramp assertions in `tests/test_mcd2100_panel.py`, and reconcile plan checkboxes with actual coverage.

## Reproduction materials

- `%TEMP%/codex_telemetry_review_probe.py`: 11 real-controller/offscreen Qt probes asserting the observed defects.
- `%TEMP%/codex_telemetry_lock_probe.py`: isolated process fixes a legal lock interleaving at method boundaries.

These probes describe the failing implementation; regression tests must assert corrected behavior. Re-review is limited to fixes and their affected scope. Optional suggestions: none.

## Final verification

Luna corrected the nine groups and the misplaced test code. Independent Astra re-review converted the original 11 probes into correct-behavior assertions: 11/11 passed. The isolated lock-order acceptance also passed with orderly drain/shutdown. Telemetry and panel suites passed 56/56 at that checkpoint. The whole-refresh error-duration probe confirmed no completion record before the second group actually drained.

Targeted follow-ups closed the remaining monitor timestamp propagation and plain-snapshot generation-check omissions: remaining acceptance probes 2/2 passed; final stale-generation regression 1/1 independently passed after source inspection of both receivers. Luna's last affected panel run was 45/45; the preceding panel plus telemetry run was 57/57. Earlier combined seven-module run was 200/200 before the last two focused regressions were added. No unsupported claim of a fresh 202-test combined run is made.

No real hardware, real App launch, computer-use, commit or speed benchmark was performed in implementation/review. Optional fast/slow telemetry grouping remains out of scope. The subsequent automatic ramp-cache phase starts only after this PASS.

### Logging handoff correction

While preparing the user-run log instructions, root found that the adapter's per-RPC buffer was present but its required DEBUG output was missing. Astra confirmed that the earlier finding-9 closure was incomplete. The sole Luna writer subsequently added success/error per-RPC DEBUG records without SDK arguments and queue wait to the existing controller DEBUG record. Its focused logging assertions are included in the automatic-cache stage's adapter validation and concentrated review; the rest of the telemetry acceptance is unchanged.
