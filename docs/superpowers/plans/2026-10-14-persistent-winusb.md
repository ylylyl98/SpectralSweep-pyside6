# Persistent WinUSB Implementation Plan

> Execute inline against the current integrated checkout; preserve unrelated
> user changes and the frozen diagnostic sources. Review native changes before
> hardware validation.

**Goal:** Reuse one initialized ST133 native session for explicit single frames.
**Architecture:** Pinned x86 helper, serialized stdin requests, immutable per-frame
evidence, conservative STOP/drain/close barriers, Python session owner.
**Tech Stack:** C/Win32 x86, original native DLLs, WinUSB shim, Python/PySide6.
**Spec:** `docs/superpowers/specs/2026-10-14-persistent-winusb-design.md`

## Global constraints

Keep 100 ms / 512 x 1 / one exposure; no cooling, PIXIS, optics or driver changes.
No force kill or automatic retry. Retain uncertain ownership. Do not modify the
frozen source worktree, old release, or diagnostic receipts.

## Task 1: Native reusable frame lifecycle

- [x] Add `tools/winspec/persistent/frame_test.c`: initialize once, complete and
  consume four changing frames, drain each, rearm only after success, compare
  bytes and verify no additional sizing or live handle leak.
- [x] Compile test with a rejecting rearm stub and observe the rearm failure.
- [x] Implement `frame.c` and `legacy.c` extensions around frozen sources:
  `BOOL ws_pipp_frame_rearm(ws_pipp_frame *)`, private dispatcher opcode 12.
- [x] Re-run existing race/short-frame tests plus new rearm tests.

## Task 2: Persistent native helper and pinned build

- [x] Add hardware-free integration tests sending `C sequence uuid` then `Q`.
  Assert only one identity/configure stage, changing per-frame data, clean exit.
- [x] Add `helper.c`, observer reset, strict command parser, frame receipts and
  EOF/close cleanup with idle connection retention. Reuse frozen runtime_setup/native_step implementation.
- [x] Build a new runtime and manifest with `build_persistent_winusb.py`; link
  only fake transport in test binaries. Test stale/short/error frames and EOF.
- [x] Independent native code review; address findings before physical use.

## Task 3: App owner and lifecycle integration

- [x] Test a `PersistentCaptureBridge` with two requests and one process, bad
  sequence/UUID/length, cancellation, missing close proof and retained ownership.
- [x] Implement bridge with `preflight`, `capture`, `is_busy`, `close`; verify
  active ownership and immutable response receipts before returning bytes.
- [x] Wire LocalWinUSBSetup to the new bridge and close it on disconnect; retain
  fixed controls and existing request/hash checks. Test CSV provenance.
- [x] Run relevant WinUSB/WinSpec/controller/UI regression tests and review.

## Task 4: Bounded hardware validation and documentation

- [x] Install new runtime alongside v1. Run at most six serial frames, stop on
  any failure, verify first/warm timing, separate hashes, drain and final close.
- [x] Compare protected driver/configuration/process snapshots before and after.
- [x] Record measured timing and limits in `docs/local-winusb-ingaas.md`.

## Completion evidence

Reviewed p007 native candidate; real fake-transport integration and 555 relevant
Python tests passed (4 skipped). Installed v2 separately. Six physical requests
shared one initialized helper: first 2.358 s, subsequent median 0.224 s. All
per-frame bytes/cleanup and final close passed; protected snapshots unchanged.
See `tmp/winspec-driver-rollback-20261013/persistent-validation-01/result.json`.
Physical freshness remains explicitly unverified without controlled illumination.

## Follow-up: approved log rotation and cache integration

Preserve one connected native session while removing the legacy 64 MiB log
admission stop. Rotation takes place only between completed frame requests.

- [x] Native synchronized writer, numbered segments, flush/close/seal/state
  publication, no per-record stdout copy, per-frame segment receipts.
- [x] Capability handshake and configured threshold validation before capture;
  retain legacy protocol behavior for older runtimes.
- [x] Index frame-to-segment relationships and reclaim only sealed logs whose
  dependent frames have been durably saved and reclaimed. Protect initialization,
  current/final logs and failed sessions. Cover interrupted deletion/restart.
- [x] Adapt optical and timing diagnostics to follow receipt log segments.
- [x] Independent review; fix log failure immediately before native START.
- [x] Complete p010 native fake-transport regression and explicit rotation
  failure tests, then broader application regression.
- [x] Install a separately pinned v4 runtime alongside v3 and record verification.
  Leave running measurement processes and physical acquisition untouched.

Verification: p010 build and 1000-frame fake-transport regression passed;
8 rotation tests passed, including repeated rotation, bounded export/cache
integration and pre-START/rotation failures. Final application selection:
699 passed, 4 skipped in 126.48 s, exit code 0. Installed runtime v4 was hash
verified, original user patch passed reverse-check, protected process start
times stayed unchanged. No physical acquisition was performed for this revision.
