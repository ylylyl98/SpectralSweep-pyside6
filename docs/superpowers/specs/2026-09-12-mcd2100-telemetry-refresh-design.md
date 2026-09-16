# MCD2100 telemetry refresh design

## Goal and scope

Make display telemetry responsive without duplicating hardware reads or weakening safety checks. This is a follow-on to the magnet-preparation work currently being edited in the main conversation. Implement only after that work and its review are complete, against its final interfaces. Preserve the 1.67 K fix, mode preparation/recovery, SDK ownership, cancellation and shutdown behavior.

First release: coalesce display refreshes; share automatic/manual display reads; completion-based background scheduling; show partial results and last-success age; measure queue, SDK and whole-refresh time. Fast/slow telemetry groups are a later evidence-driven change, not part of this implementation.

## Current evidence

The panel refresh reads a full magnet snapshot, then a temperature snapshot. With available SDK methods this is 11 plus 8 synchronous RPCs. The default owner polling interval is 0.5 s. The refresh slot has no in-flight guard. SDK access is serialized. These are source observations, not measured device latency. A slow individual RPC cannot be accelerated by request coalescing.

## Required behavior

- One controller-owned display read per group (magnet or temperature), shared by display subscribers.
- A repeated click while a display refresh is pending joins existing work; it does not add a trailing refresh.
- A manual refresh can use a successful display read from the same connection generation completed within 0.5 s. Otherwise it joins an in-flight display read or submits a new one.
- An automatic cycle schedules its next attempt 1.0 s after its last read has drained, never tries to catch up missed ticks, and defers while measurement, preparation, shutdown, recovery restrictions or priority control work require the owner.
- Safety and workflow reads remain fresh, noncached, noncoalesced APIs. No safety decisions consume display cache. Keep existing safety sampling frequency and semantics.
- Magnet results display as soon as they arrive; temperature follows. A temperature failure does not erase a successful magnet result.
- Show separate last-success ages for magnet and temperature. Label disconnected data as last-known, not live. Refresh completion is not evidence that all individual RPC readings have the same acquisition time.
- A timed-out client future does not free a display slot until the underlying request is drained. No callbacks from an old generation may update a reconnected panel.
- All SDK calls remain on the existing owner QThread. No extra SDK socket, thread, or vendor-library edits.
- Stop and control requests retain priority. Display refresh never issues Stop, changes controller safety state, or allows a mutation during unresolved mode recovery.

## Diagnostics

Use monotonic timestamps for enqueue, start, finish and drain. Record request ID, connection generation, group, source, queue wait, execution time, result/error and cache/join disposition. Record individual SDK method duration in a bounded diagnostic buffer (256 records) and DEBUG logger. No disk files by default; no host credentials, SDK arguments or sample identifiers in diagnostics.

SDK timing covers actual RPC invocations once, including exceptions, without swallowing or translating errors differently. Whole-refresh timing runs from user request to both groups terminal/drained. Keep successful and failed durations, and do not assert wall-clock thresholds in unit tests.

## Validation and boundaries

Use event-gated fake adapters, injected clocks and offscreen Qt. Verify call counts, ordering, SDK-thread identity, freshness boundaries, reconnection generations, timeout/drain and priority. Test overlap with temperature-apply monitoring so another display monitor does not independently duplicate temperature reads. Physical speed improvement must remain unclaimed until per-RPC timing is measured on the user's instrument; this task does not authorize hardware calls.

Only new specification/plan documents are written in this side conversation. Luna execution and Astra review must be dispatched in the main conversation, not here.
