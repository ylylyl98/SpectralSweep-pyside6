# Local review fixes implementation plan

**Goal:** Resolve the five reproduced findings from the local-version review and make their regression tests isolated and reliable.

**Design:** Extend the existing acquisition cancellation path through flipper preflight; keep metadata failure and identity bookkeeping within each run; bound filename preview before expanding loop combinations. Preserve existing setup routing and saved-session formats.

**Stack:** Python 3.13, PySide6, pandas, SQLite, pytest.

**Approved requirements:** The user requested implementation of the preceding review findings. Existing local changes must be preserved. No live hardware operations, commits, or application restarts are required.

## 1. Acquisition cancellation

- [x] Add a failing regression covering actual Spectrum Run/Stop while USB readback is pending, plus queued/2D cancellation and a fresh acquisition after cancellation.
- [x] Give each acquisition its own cancellation event, propagate it into flipper routing, and check it immediately before detector acquisition. Cancel pending requests through the existing abort entry point.
- [x] Run the new regressions and existing MFF101/controller/measurement-ownership suites.

## 2. Metadata integrity

- [x] Turn the three isolated repros in `tmp/local_review_metadata_repro.py` into assertions of desired behavior, and confirm failure first.
- [x] Mark recorder/storage failures as degraded while preserving detector cleanup and ordinary optional-read handling.
- [x] Allocate compatibility settings references consistently across recorders sharing a run; retain correct primary snapshot and journal links.
- [x] Rebuild reopened-run counters using the same capture semantics as live recording, preserving recent IDs and output associations.
- [x] Run metadata, event recorder, Andor, history and finalization regressions with temporary SQLite databases.

## 3. Bounded filename preview

- [x] Add a regression proving a very large Cartesian draft never enters the unbounded planner while updating filename previews.
- [x] Reuse a bounded draft preview or stop before full expansion, retaining ordinary filename, filtering, selection and nesting behavior.
- [x] Run loop editing/selection, filename, sequence and UI performance regressions.

## 4. Test isolation and route coverage

- [x] Install a pytest-wide temporary history override before test collection, and use per-test databases so no normal user history is written.
- [x] Update sidebar/map tests to observe output routing through actual acquisition preflight instead of the obsolete activation hook.
- [x] Exercise failed preparation with the real controller path, retaining measurement locks and matching sidebar state.

## 5. Integration and review

- [x] Review this turn's scoped changes against the saved snapshots.
- [x] Run combined relevant regression suites, inspect all failures, and run whitespace/compile checks.
- [x] Obtain an independent code review and resolve actionable findings before reporting completion.

Use `.venv-pyside6-313/Scripts/python.exe -m pytest` for tests. Keep all simulated outputs/history under pytest temporary paths. Do not call vendor drivers or the running application.

## Review follow-ups

- [x] Filter queued capture frames, readbacks and errors at GUI delivery using the request's cancellation event, including cancellation after the worker has already emitted its result.
- [x] Release Spectrum ownership when pending-request cancellation is accepted but the detector Abort operation raises, while retaining the unconfirmed-stop diagnostic.
- [x] Stop the active automatic calibration collection through its existing completion signal when Spectrum Abort cancels its capture.

## Verification evidence

- The four new regression files passed together: **43 passed**, covering cancellation (22), filename bounds (12), metadata integrity (8), and history isolation (1).
- Cross-module routing/preflight integration: **99 passed**, including sidebar, maps, Dual Gate, LightField validation, metadata, and MFF101 measurement routing.
- Existing loop, sequence, filename, and UI performance regressions: **104 passed, 3 subtests passed**; the final two additional large-panel cases also passed in the complete filename suite above.
- Metadata/event/history/Andor regression group: **139 passed, 22 subtests passed**.
- Device/Spectrum group before review follow-ups: **206 passed, 22 subtests passed**. The independent reviewer then verified the final cancellation, ownership, and standalone WinSpec paths: **51 passed**.
- Scoped `git diff --check` and `py_compile` passed. The independent review has no remaining actionable findings. Counts from different groups overlap and should not be summed.

All tests used simulated devices and temporary history databases. This work does not repair already-corrupted legacy compatibility references or confirm live USB/vendor behavior.
