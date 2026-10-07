# WinSpec second-program timing investigation

> **For agentic workers:** Use the executing-plans workflow for the checks below. This is an offline investigation in the existing workspace, with an independent read-only review before reporting results.

**Goal:** Attribute the remaining second-program cost inside Start and identify a concrete next optimization backed by existing evidence.

**Architecture:** Reuse the frozen paired-output trace and the accepted production light traces. Verify source hashes, correlate output call stacks with frozen PE disassembly, and reconstruct candidate bulk payloads offline. Keep acquisitions from different runs separate; debugger pause subtraction is not native execution time.

**Tech Stack:** Python 3, existing pefile/capstone dependencies, saved XP QPC traces.

**Spec:** User-approved continuation of the second-program (~275 ms) investigation; preserve complete hardware command semantics and prefer existing evidence over additional acquisition.

## Constraints

- No production behavior changes, DLL patches, deployment, or new camera acquisition in this investigation.
- Preserve all earlier frozen evidence and unrelated working-tree changes.
- Report traced durations as observations, never as predicted savings.
- A matching output sequence is not proof of hardware timing equivalence or persistent device state.

## Task 1: Reproducible offline attribution

**Files:** Create `tmp/winspec-performance-20261001/second-program-analysis-20261005/analyze.py`, `test_analysis.py`, and generated JSON evidence.

- [x] Verify the old communication evidence and current production evidence against their manifests (215 + 805 files).
- [x] Recompute program, output-call, and serial-block intervals; report debugger pauses and resume uncertainty separately.
- [x] Correlate the 128-call `0x4A` group to the two caller branches and decode the pulse data without dropping repeated values.
- [x] Verify frozen imports, exports, batch packet assembly, transport timeout, and return-value behavior.
- [x] Check saved pulse streams and malformed/truncated streams with five focused offline tests; generate a deterministic report.

## Task 2: Review and document the next decision

**Files:** Update `docs/winspec-side-port.md`; create evidence manifest and read-only review record under the new analysis directory.

- [x] Independently review attribution, timing limitations, exact pulse preservation, and the batch API's error contract (read-only review passed).
- [x] Document the concrete candidate and the remaining prerequisites for a short live experiment.
- [x] Re-run analysis and verify final artifact integrity before reporting completion; deterministic reports and five tests pass, 19 bridge plus six host/deployment files remain unchanged.
