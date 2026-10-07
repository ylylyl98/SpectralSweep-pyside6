# Combined WinSpec Start implementation plan

> Use executing-plans to implement and verify each task. The user approved the
> integration, offline regression, short hardware comparison and conditional
> formal integration sequence in this chat.

**Goal:** Preserve the existing first-body state gate, batch only current second-
program pulses, and measure the combined result before enabling it in the formal
opt-in path. No automatic retry of acquisition or uncertain I/O is permitted.

**Design:** Leave the original program callback, stack and endpoint instructions
unchanged. Intercept only the two output imports. The three existing debugger
endpoints publish phase and original frame identity; each native call validates
that frame in its bounded EBP chain. Authorize only at the SECOND body endpoint,
after first redirect/TF/return/tail and current second state have all passed.
Use an independent, nonzero native generation, never resident frame_id as nonce.
Every frame clears permission. The first handshake consists of two preserved
outputs; neither it nor either 520/512-byte original Multiple call is omitted.
Validate the second program's current call sequence and pulse triplets instead
of replaying or comparing pulse values from an omitted first body.

## 1. Native compatibility and authorization

- [x] Freeze the prior accepted sources; create an isolated combined validation directory.
- [x] Add failing native tests for original-stack scope, stale/missing permission,
      changed current data bits, malformed pulse order and timeout retention.
- [x] Implement import-only hooks and current-frame pulse checks, retaining the
      checked WriteFile completion path and persistent pending buffers.
- [x] Test debugger authorization memory writes, frame nonce reset and the exact
      preserved two-output first prefix without relaxing the existing Gate.
- [x] Independently review before any hardware acquisition.

Review fixes in the isolated prototype: retain the suspended debug event on
uncertainty, including marker-write failure; keep the release guard until the
final exit TF/readback finishes; reject pre-Continue clock failures before the
WinAPI release; distinguish post-Continue clock loss; keep native identity fields
stable while arming. The actual XP synthetic debugger/DLL test uses real redirect,
TF and shared-memory writes, and replaces only SDK snapshots/caller fixtures.
It checks a successful 375-byte transfer and uncertain permission after the write.
Synthetic cleanup terminates the owned debugger before its suspended target.

## 2. Short hardware comparison

- [x] Run XP compile and camera-free tests of the exact frozen DLL and scripts.
- [x] Compare ordinary, existing-first-only and combined paths; exclude full
      baseline acquisition from stable timing cohorts.
- [x] Verify per-frame first-body redirect, five second-program packets, SPE/raw/
      panel data, Stop rejection, restart and parameter invalidation/rebaseline.
- [x] Restore original recipe, flags and hooks; confirm debugger detach, normal
      WinSpec exit and absence of a remaining owner.
- [x] Recompute medians and packet/data checks independently; preserve failures
      as well as acceptance evidence. Do not add separately measured savings.

## 3. Conditional formal integration

The v4 comparison and v5 Stop/restart supplement passed independent review.
Median SDK Start: ordinary 0.618610 s, first-only 0.384683 s, combined
0.215711 s. v4 verified 40 native packets and 24 delivered SPE/raw payloads;
v5 preserved all debugger archives and verified the Stop/restart gap separately
(15 packets, six delivered payloads). UI measurements use offscreen widgets and
an optics fixture. The Stop supplement interrupts acquisition after native
programming completes; uncertain native writes remain camera-free fault tests.

The formal candidate was developed and accepted under
`tmp/winspec-performance-20261001/combined-start-formal-20261005` before publishing
the exact reviewed package. It reuses the DLL, one control buffer, and one
native event; retirement durably archives records before acknowledged reset.
Native tests include 121 frames across record resets and 2,000 event-reuse cycles.

- [x] Only after acceptance, incorporate the helper and authorization into the
      existing opt-in acceleration lifecycle, with disable/release restoration.
- [x] Run focused production tests and XP compatibility checks, review the final
      implementation, and perform a short acceptance of the final frozen sources.
- [x] Back up and update the bridge through its established checked deployment
      procedure; retain rollback files and leave the bridge stopped afterward.
- [x] Document measured limits and verify source/deployment/evidence hashes.

Formal v3 passed independent acceptance: 18 delivered spectra plus one rejected
Stop; 40 exact native packets, 96 state/ROI snapshots and eight resource-retirement
groups. Same 500 ms x2 Start medians were 0.617799 s ordinary and 0.215975 s combined.
The normal frame limit stays 30; the short test used two to exercise rollover.
The earlier formal harness failures (Python 2 None-module sentinel; observer
lock contention during a read) remain archived; neither changed production core.
The accepted package passed 174 production tests (four skipped), and its 27 files
were installed with matching hashes. XP backup:
`C:\WinSpecRemote\before-start-acceleration-20261005-160341`.
The final process check at 2026-10-05 20:03:49 UTC found no WinSpec/Python owner;
the bridge was left stopped. See the independent report and installation receipt
in the formal evidence directory.
