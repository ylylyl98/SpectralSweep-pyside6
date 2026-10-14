# Local WinUSB exposure, accumulation and latency

User authorized both previously proposed steps: support and validate different
exposure/accumulation, and measure/optimize acquisition, handoff, save and cleanup.
Work continues on the existing uncommitted integration without discarding it.

## Contract

- Manual 512 x 1 acquisition; integer exposure 10..1000 ms, accumulation 1..100.
- Native initialization sets one exposure per session using the existing native
  double setter (seconds), before full initialization. The ready and frame
  receipts identify that exposure and a negotiated recipe protocol.
- Unchanged exposure reuses the same native session. A requested exposure change
  normally closes the old session and starts a new one; no in-place setter after
  initialization and no automatic retry after any acquisition fault.
- Accumulation is sequential host summation in uint64 and division by N for
  displayed mean counts per exposure. Every uint16 subframe, UUID, sequence,
  session, exposure and payload hash remains in the exported acquisition snapshot.
- Cancellation publishes no partial mean, issues no additional capture after it
  is observed, and preserves unacknowledged subframes. Explicit reconnection is
  used after a bridge fault. No force termination or owner-claim removal.
- Save acknowledgement verifies every raw subframe and the aggregate; only a
  completed, durable matching export makes any constituent eligible for cleanup.
- Existing single-frame exports and installed v4 protocol remain compatible.
- Enable manual exposure/accumulation controls only for advertised capability.
  Continuous acquisition and wavelength scans remain disabled: wavelength and
  temperature qualification are separate outstanding requirements.
- Timing records separate preflight, cache cleanup/gate, initialization, request
  publication/wait, evidence validation, index recording, export and save ack.
  Optimize only measured overhead without removing identity/hash/ownership/
  drain/save guards. Native timing is not calibrated physical exposure timing.

## Validation

Failing behavioral tests precede implementation. Fake native transport verifies
multiple exposures, protocol mismatch, sequence/hash corruption, accumulation
overflow avoidance, cancel/disconnect and cleanup of all/none of a saved batch.
Run native tests serially because the shared startup mutex is global. Independent
review must precede physical execution of a new binary. Install a separately
pinned v5 runtime while retaining v4.

Bounded real tests use unchanged illumination and no optics/cooling commands,
with protected process/driver/config snapshots before and after. Compare 10,
50,100,300,1000 ms (exclude saturated pixels from response fits), accumulations
1,2,5,10 and a cancellation/reconnect sequence. Preserve results and any failure;
never automatically retry a failed capture. Profile matched 100 ms sequences
with identical rotation/retention/export settings before and after optimization.

Physical pixel freshness and photometric linearity remain limited by uncontrolled
illumination, unknown physical temperature and uncalibrated exposure timing.
