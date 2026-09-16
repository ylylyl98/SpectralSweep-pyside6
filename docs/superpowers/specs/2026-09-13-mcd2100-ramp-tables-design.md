# MCD 2100 read-only ramp tables

Date: 2026-09-13. Sequence: Astra plan, Luna implement, Astra review. This follows the reviewed magnet-preparation work; preserve all current changes. Do not modify or execute the separate telemetry-refresh plan in this stage.

## User outcome

An explicit `Read ramp tables` button in MCD 2100 reads the current and factory-default raw ramp tables through the App's existing connected attoDRY2100 controller. A compact native dialog shows two tabs, `Current` and `Default`, with Channel, Requested index, Raw range, Raw rate and Error columns. Values are read-only. Show reported counts, errors and read status; keep successful rows when other reads fail. Opening the panel, connecting, refreshing telemetry, preparing the magnet or starting MCD must never invoke this operation automatically.

## Evidence and interpretation boundary

Local `CRYO2100/magnet.py` and configured runtime `D:/Insturment control v3/CRYO2100/magnet.py` expose:

```python
getNumRampRates(channel) -> int
getRampRate(channel, index) -> (range, rate)
getNumDefaultRampRates(channel) -> int
getDefaultRampRate(index, channel) -> (range, rate)
```

The default row reader has reversed argument order. Generated docstrings do not establish index base, rate/range units or preset names. Enumerate requested raw indices `0..count-1` only; label this as an explicit diagnostic enumeration with unverified index base. Do not probe extra indices or infer a one-based retry, `Fast`, or index 5 meaning. A successful call reports what that requested index returned; it does not prove complete index coverage when the indexing convention is unknown.

The user-provided `D:/Dropbox/porject abstract/Setup testing/Attocube 2100 Setup/atoodry2100 disk/Manuals & Specifications/01_220507_System-Spec-Sheet.pdf` identifies APS100 on page 5 and magnet specifications on page 8: 2044.9 G/A; charging 0.0344 A/s over 0–40 A and 0.0172 A/s over 40–44.0116 A. These are magnet specifications, not documentation of SDK raw units/index mapping. No unit conversion or numerical comparison against those values belongs in this UI.

## Fixed constraints

- Preserve every existing working-tree change, including the 1.67 K fix and reviewed magnet preparation.
- Use the existing adapter, connection and owner QThread. Do not construct a new SDK device or socket.
- The only new vendor calls are the four getters listed above. No setRampRate, mode mutation, field set/start/Stop, rollback or automatic recovery operation.
- No real hardware reads or tests during implementation or review. Only a later explicit user button click triggers a real read.
- Display raw values without unit labels or preset inference. Record method, channel, requested index when applicable and error text.
- Keep the telemetry-refresh feature untouched; no new periodic reads.
- No commits, new frameworks or unrelated refactoring.

## Data and bounded read policy

Use immutable dataclasses alongside existing adapter result types. A report includes channel, monotonic timestamp, two table results and an interrupted flag. Each table result contains kind, reported raw count, attempted rows and count/table errors. Each row retains requested index, raw range/rate and optional error. Invalid row payloads retain their repr in the error instead of manufacturing a numeric value. Do not round numeric values before storing them.

Require counts to be actual Python integers (not bool), nonnegative and at most 32 per table. This is a software diagnostic bound, not a claimed firmware limit. A malformed/oversized count is displayed as reported with an explicit error; read no rows for that table and attempt the other table if the request remains within its time budget. Zero is valid and shown as an empty table. Read each admitted row once; retain individual row failures and continue bounded enumeration. No retry loops. Catch Exception, not BaseException.

The normal controller request watchdog remains active for the aggregate read. It must set a read-only cooperative cancellation event on timeout; between calls the adapter stops scheduling further getters and returns its partial report. An in-flight SDK call cannot be forcibly cancelled: client timeout and owner drain remain separate. A late report can be retrieved from the drained acknowledgement for display, while the operation retains its timeout status. Neither read errors nor timeout use magnet Stop or discard previous successful rows. A hung call keeps shared controls locked until existing owner drain semantics allow release.

## Admission and UI lifecycle

Connected and idle means no preparation, measurement, temperature application, shared external workflow, pending SDK work, mode recovery or shutdown. Controller `IDLE` is admitted; controller `ACTIVE` may also be admitted when no App workflow is active (e.g. holding Start after standalone preparation), because field-control-active alone is not an acquisition lease. Panel workflow state supplies that admission information; do not issue Stop just to make this read available.

Reserve the read in the controller lifecycle lock before enqueue. Block overlapping mutations/preparation while its request is queued, running or draining. Panel holds the existing shared-instrument interlock until this read drains, guarding Start/Prepare/disconnect/temperature-apply handlers as well as buttons. Reject duplicate reads. A diagnostic read never claims field ownership or changes magnet-preparation recovery flags. Restore the previous controller state after a read timeout drains; do not leave a false TIMED_OUT_DRAINING state indefinitely.

The nonmodal native dialog can remain open while data loads. Closing it does not close the instrument, send Stop, or release the read interlock early. Panel owns the operation handle/results; reopening the result dialog shows retained values. The button starts one explicit fresh read when admitted. During the read, show `Reading ramp tables…`; after errors show `Read finished with errors` or `Read timed out; partial results retained`. Show the caveat `Raw SDK values; units and index base are unverified. Requested indices: 0 through count − 1.` No automatic reread on connect or dialog opening.

## Acceptance

Fake SDK tests prove signature order, bounded counts, raw preservation, current/default independence and zero vendor writes. Controller tests prove owner-thread serialization, admission races, timeout/drain retention and no Stop/close side effects. Offscreen Qt tests prove explicit-only invocation, independent error display, slot guards, shared interlock and no premature unlock. Existing magnet-preparation and MCD suites stay passing. Delivery states that actual tables have not been read.
