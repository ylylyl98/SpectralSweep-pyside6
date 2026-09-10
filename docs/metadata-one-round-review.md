# App-wide metadata: one-round implementation review

## Outcome

The Luna implementation pass and Astra review identified the defects below. The repair pass now addresses the recorder, acquisition relationships, shared setup provenance, and viewer gaps in the working tree. This report remains the design and audit record; current behavior is described by the disposition notes below.

The original partial implementation added typed arrays, a shared recorder, configuration snapshots, and initial tab integration. The subsequent Luna repairs add the recording guarantees, capture relationships, compatibility projections, and viewer behavior described below.

## Validation

- Baseline: 513 existing tests passed in the independent full-suite run.
- Repair focused suite: 77 tests passed, including recorder recovery/race/checkpoint cases, compact compatibility-state replay, LF capture IDs, BFP frozen inputs and aggregation membership, Spectrum asynchronous settings snapshots, and ND filename behavior.
- Final independent isolated full suite: 527 tests passed in 68.761 seconds using `.venv-pyside6-313`, including LightField readiness and compatibility-state reconstruction. A real offscreen Motion Sweep widget check also confirmed changing Vbg from 1.25 V to 2.5 V immediately updates the filename preview.
- Whitespace validation: `git diff --check` passed.
- Review used offscreen UI tests, fake devices, and temporary files only. No instruments were operated.
- Before repair, a synthetic benchmark with a 2,000-position plan and 100 capture events performed 100 full manifest/SQLite writes, taking 2.918 seconds. The final recorder benchmark performed five full writes in 0.426 seconds; its manifest was 79,396 bytes and journal 32,572 bytes. The event payloads differ slightly; these are synthetic measurements, not hardware acquisition benchmarks.

## Prioritized defects

### P1: Spectrum can save settings belonging to a later operation — repaired

`ui/spectrum_panel.py:401` replaces `_acquisition_settings_snapshot` when settings are applied, independently of cached spectrum data. Reproduction: acquire at 10 ms, apply 999 ms without another acquisition, then save. The saved sidecar attributes 999 ms to the original spectrum. The saved data needs its own acquisition-time snapshot, including externally pushed data.

### P1: Failed metadata appends can still become completed experiments — repaired

`app/experiment_metadata.py:432` increments acquisition state before appending the event. Append errors are caught and returned as `None`; terminalization does not require a complete journal. A forced append failure produced a completed run with one counted acquisition and no event file. Recording failure must be visible and must not claim successful complete metadata.

### P1: Event recording rewrites the growing main JSON on every event — repaired

`app/experiment_metadata.py:449` calls `_register_event_file()`, and line 473 calls `_write()` even when the event file is already registered. The main JSON also accumulates acquisition IDs and output mappings. This defeats the intended append-only design and grows acquisition overhead. Settings are duplicated in canonical strings, values, and LightField snapshots.

### P1: BFP input hashes can describe different data from the computed result — repaired

`ui/bfp_panel_integrated.py:754` and `:1091` register/hash inputs at first export rather than computation. Computing RC, replacing the sample CSV, and exporting yields cached original output but hashes the replacement input. Freeze input identities/hashes with the computation. Also preserve explicit sample/background roles, portable references, and links to parent experiments; absolute event paths and basename-only settings are insufficient.

### P2: MCD 1000 readbacks are attributed to condition 1 — repaired

`ui/mcd_panel.py:1071` reads the condition index from `self._p`, while `_run_condition()` updates a local copy and `self._condition_index`. A fake condition-2 run emitted `condition-1`. Use the active condition consistently.

### P2: Resuming a partial journal loses the next event — repaired

`app/experiment_metadata.py:441` appends directly after an incomplete final JSON line. A new event becomes part of the malformed line and is not returned by the reader. Recovery must preserve valid existing events and separate or quarantine the incomplete tail before appending.

### P2: Multiple reopened handles are independent competing writers — repaired

`app/experiment_metadata.py:744` creates a fresh run handle with an independent lock, stale metadata, and its own event counter. Two handles can both write event index 1, and the second manifest can overwrite the first handle's acquisition accounting. Enforce single-writer ownership, including late-export paths.

### P2: DataFrame serialization remains unsupported — repaired

`app/experiment_metadata.py:97` traverses pandas internals as generic object state. Serializing a simple DataFrame raises a TypeError involving Flags/ReferenceType. Add an explicit tabular representation with complete values and column information before relying on it for exact plans.

## Agreed-plan gaps

1. **Dual Gate applied plan — repaired:** run settings include explicit serialized executed sequence, DataFrame batch table, and acquisition schedule, while the event stores a bounded plan reference.
2. **Capture/output relationships — repaired:** LF recorder UUIDs flow into MCD observations; BFP freezes successful repeat IDs and records them with the averaged output; MCD and 2D condition/map events link outputs and rows.
3. **Competing metadata sources — repaired as compatibility projections:** 2D `.meta.txt` and MCD detailed JSON remain for older tools, while shared run events own condition/map details, timing, cleanup, readbacks, and output links.
4. **History completeness — repaired:** the viewer has bounded event paging/filtering and the formatter includes setup, plan, conditions, acquisitions, instruments, calibration, observations, files, and status.
5. **Shared settings and instruments — repaired in integrated tabs:** run start records normalized controller inventories; worker paths add observed/applied readbacks where available, with unknown values left unavailable. Motion/BFP/Spectrum include correction/calibration provenance relevant to their data.

## Status of the requested architecture

The target architecture is implemented in the working tree: one authoritative experiment JSON, one shared per-run event log, and linked data files, with separate linked records for processing that combines multiple experiments. Compatibility files are derived projections when a shared run is present; standalone workers retain their legacy behavior when no shared run exists. No hardware was operated.

Source line numbers refer to the working tree at the end of this review and may move in later edits. Existing ND-calibration changes and the user's `test_ntfy_notification.py` were preserved.
