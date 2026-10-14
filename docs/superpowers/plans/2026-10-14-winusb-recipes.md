# WinUSB recipe and latency implementation plan

**Goal:** Implement and physically validate manual variable exposure and safe
software accumulation, with measured latency improvements.

**Architecture:** Versioned exposure handshake over the existing native session;
host-side accumulation with complete subframe evidence; transactional save
acknowledgement of constituent frames. Change exposure by orderly session renewal.

**Spec:** ../specs/2026-10-14-winusb-recipes-design.md

**Constraints:** Preserve frozen vendor sources, v4, existing measurements,
unsaved files, driver/optics/cooling state. Never kill/retry an unresolved helper.
No commits/pushes authorized by this task. Execute native tests serially.

- [x] Profile current v4 with wrapped stage timers and identical durable export
  and small retention window; save per-call stages and protected snapshots.
- [x] Add failing native recipe tests plus Python protocol/invalid-input tests.
  Extend the private generated native source and helper command/receipts; build
  fake transport candidate and test session-local exposure and clean close.
- [x] Add failing accumulation tests for sum/mean, overflow, correlation,
  cancellation and lost/changed subframes. Implement bounded recipe validation,
  setup accumulation and exported batch metadata; keep single frame compatible.
- [x] Add failing retention tests for fully saved batch versus changed/partial
  batch; implement constituent validation and transactional acknowledgement.
- [x] Add timing-stage assertions and measured-overhead regression tests; apply
  only evidence-supported optimizations. Enable manual controls by capability.
- [x] Run relevant Python/native/GUI regression suites and independent review.
  Resolve important findings before installing a separately pinned candidate.
- [x] Run bounded exposure/accumulation/cancel/reconnect real tests and matched
  latency comparisons, verify normal close and protected-state equality, append
  results/limits to existing documentation; recheck Git upstream/status.

Results: v5 p011 installed alongside retained v4; 75 real frames / 45 saved
spectra / 73 acknowledged constituents, with two cancellation frames retained.
Seven normal closes; protected state unchanged. Independent archived-payload,
CSV and receipt revalidation passed. Native offline regression: 39 passed;
targeted Python/Qt regression: 366 passed / 21 native opt-in skips (covered by
the separate native invocation). Full-repository run intentionally stopped.
Final 40-frame profile: warm capture median 0.56916 s versus 0.65584 s baseline;
capture/export/ack median 1.17314 s versus 1.26674 s. Full evidence and limits are
in docs/local-winusb-ingaas.md. Automated scans remain unavailable under the
agreed temperature/calibration/optical qualification constraints.
