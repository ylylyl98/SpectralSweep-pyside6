# WinSpec short repeated Start validation

> Execute inline using executing-plans; reuse the independent code reviewer. The user approved the proposed repeated comparison and then shortened the hardware run. No production enablement or multi-hour acquisition is authorized by this plan.

**Goal:** Check repeatability of the conditional first-program omission across repeated acquisitions, selected exposure/accumulation changes, and clean application restarts.

**Architecture:** Copy the frozen successful diagnostic into a new isolated tmp directory. Keep the debugger, state gate, real handshake, second complete program, temperature guards and file validation unchanged. Parameterize only the acquisition recipe and wrap the existing ten-frame block. Each block has nine normal frames and one conditional optimized frame with its own freshly learned baseline. Stop subsequent blocks on failure, fallback, pending document/recovery, or elapsed time limit.

**Tech stack:** XP Python 2.7 / frozen WinSpec binaries; host Python 3.13 and pytest.

**Spec:** User-approved short test in this conversation and the completed first-program experiment plan.

## Scope and limits

- Initial ceiling: 30 blocks in three sessions. After the user repeatedly clarified that endurance was not required, stop after the first two successful sessions covering all four recipes and a clean reconnect: 20 blocks, 200 frames, 20 actual omissions. The third frozen package is not launched for hardware. Never force-kill a running hardware call to meet a time budget.
- Recipes: 500 ms × 2, 250 ms × 2, 800 ms × 1, 1000 ms × 1; sequential frames 1, unchanged ROI/ADC/gain. Restore original recipe and flags after every block.
- Only clean Quit and fresh application connection are tested. No physical disconnection, injected device faults or ROI mutation in this short run. Existing camera-free fault and mismatch tests remain required; distinguish them from hardware evidence.
- Any changed caller/state/command sequence ends this experiment; do not broaden the omission gate to obtain a pass.
- All timings contain debugger overhead. Data comparisons describe observed repeatability, not independent proof of integration accuracy or long-term reliability.

## Task 1 — Bounded runner

- [x] Add failing tests: explicit recipe reaches all ten frames and restores original; pre-existing recovery state cannot be cleared by a new block; failed/fallback/unrestored block prevents the next; elapsed budget prevents a new block.
- [x] Extend private `run_probe(..., recipe=None)` with pre-existing recovery check before overwriting ownership; report requested recipe and preserve all guards.
- [x] Add `repeat_session.py`: unique block output directories, existing durable per-frame journal, export before continuing, compact exclusive session progress snapshots, verified clean Quit even after recoverable failure, retained ownership on uncertainty.
- [x] Run new and inherited host tests, independent review, freeze source hashes, XP compile and camera-free selftests before hardware.

## Task 2 — Hardware and evidence

- [x] Check owner absence before each new session. Launch each session once from an exclusive intent; do not retry uncertain launches.
- [x] Verify every block immediately: actual omission, preserved streams, archive, recipe/temperature/restore checks. Stop on unexpected data or state and preserve evidence.
- [x] Recompute host analysis from SPE files and traces, compare each optimized spectrum with its own normal references, group timings/data by recipe. Verify distinct frame hashes and process exits.
- [x] Preserve source/evidence hashes and the host analyzer correction; document actual coverage and remaining limitations in `docs/winspec-side-port.md`.

No commit or modification of unrelated work is part of this task.

## Analysis correction

The second session exposed a host analyzer assumption inherited from the original fixed two-accumulation experiment: it required every SPE to contain signed 32-bit pixels. All frames in both single-accumulation blocks, including ordinary controls preceding the optimized frame, instead have datatype 3 (unsigned 16-bit) and 5124 bytes. Both types matched the original acquisition validator and reported data. Add failing tests for both types and for truncation, extra bytes, wrong dimensions and unsupported types, then decode by the observed SPE header. Keep per-block type consistency, exact payload length, metadata agreement, hashes and all recipe/trace checks. No XP runtime or gate changed after freezing.
