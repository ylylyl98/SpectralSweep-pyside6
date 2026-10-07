# WinSpec consecutive optimized-frame validation

> Execute inline using executing-plans and retain the independent reviewer. The user explicitly authorized verifying consecutive optimized frames; no endurance run or production enablement is part of this task.

**Goal:** Validate 30 adjacent acquisitions that each actually omit the guarded first programming body, without intervening ordinary acquisitions or recipe reapplication.

**Architecture:** Reuse the prior frozen diagnostic in a new tmp folder. Capture four ordinary frames and one traced full baseline, then 30 trial frames against that same baseline, then four ordinary frames. Keep the original gate, complete second programming call, temperature/Stop/ownership protections and archive receipt ordering. Debugger attachment is per frame and does not issue camera calls. Save full traces/configurations separately and include their hashes and compact summaries in cumulative reports.

**Tech stack:** XP Python 2.7/WinSpec frozen binaries; host Python 3.13/pytest.

**Spec:** The user's annotation selecting “验证连续每帧都优化” and request to execute it; prior short repeated validation in `docs/winspec-side-port.md`.

## Bounds

- One session, fixed 500 ms × 2, sequential frame count 1, unchanged ROI/ADC/gain.
- 39 requested frames: 9 ordinary and 30 adjacent conditional trials. No ordinary frame, recipe reapply, reset, or baseline replacement inside the trial segment.
- Require an actual redirect, complete preserved streams, successful cleanup and unique archived frame for every trial. A fallback or error ends the sequence and is not a successful continuous run.
- Do not change gate equivalence or restore cached device memory to make later frames pass.
- All timings are instrumented. Per-frame debugger attach/detach adds inter-frame gaps; the test is consecutive acquisitions, not a zero-gap continuous camera exposure mode.
- No production modifications, physical fault injection, unrelated commits or multi-hour acquisition.

## Runner and tests

- [x] Add tests proving all trial starts are adjacent, share one unchanged baseline, and have no recipe writes between them; stop on the first fallback/error; preserve owner/restore guards.
- [x] Extend only the private acquisition sequence and compact report handling; retain full raw traces and their hashes.
- [x] Independently review, freeze hashes, run inherited/new host tests, XP compile and camera-free selftests.

## Hardware and verification

- [x] Verify owner absence, launch once, monitor the bounded run and retain failed evidence if any condition fails.
- [x] Independently verify all SPE headers/types/counts/hashes and every trace against the same original baseline, prove trial adjacency and absence of inserted ordinary starts, check temperature, restore, detach, Quit and process exit.
- [x] Compare optimized spectra against surrounding ordinary references; report actual timings and count, not a guarantee of optical equivalence or long-term reliability.
- [x] Copy/hash evidence, obtain independent result review and document the outcome.

## Outcome

The sole hardware attempt completed all 39 frames and 30 adjacent actual omissions
with no fallback in 98.033 s. Optimized Start median was 0.380696 s. All trial configs
contained the same original baseline, and the source made no recipe reapplication
inside the trial segment. All SPE/temperature/restore/exit checks passed. Source and
copied evidence analyses agree; independent review verified all 346 copied files.
Host verification passed 153 tests with four skips. No production code was enabled.
