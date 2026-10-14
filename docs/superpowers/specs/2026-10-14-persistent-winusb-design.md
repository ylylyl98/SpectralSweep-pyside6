# Persistent local WinUSB acquisition

User-approved objective: retain the ST133 connection and initialize once so
subsequent explicit 100 ms captures avoid EEPROM/configuration startup cost.

The fixed 512 x 1, 100 ms recipe remains unchanged. A new separately pinned x86
helper initializes the existing native controller once, then accepts serialized
capture requests over its private stdin pipe. Each request carries a UUID and
monotonic sequence. Each result has its own directory, native receipt, raw USB
snapshot, native frame and per-frame STOP/drain/join proof. The original frozen
single-frame release and its evidence are preserved.

A successful drained and consumed frame may rearm the PIPP adapter. Rearming
destroys the previous host request/delivery objects, resets data/event flags,
and preserves geometry and native controller/USB handles. Errors never rearm.
The observer also resets only after worker joins. No parallel frame requests,
automatic hardware retry, force kill, cooling command or optics command is added.

EOF and explicit Close trigger orderly native destruction. Empty idle sessions
remain connected; a partial command times out after 30 seconds. Abort
cancels publication, performs STOP/drain, and ends the session if cancellation
prevents a complete valid frame. A failed lifetime barrier retains ownership.
The Python bridge distinguishes idle connected from acquiring, keeps the shared
owner receipt until a checked native close receipt, and rejects mismatched,
missing or altered response evidence. Close is wired into backend disconnect.

Tests must exercise real native libraries with a fake transport, not live USB:
repeated frames with changing deterministic bytes, rejection before drain,
failed/short/stale/disconnected transfers, EOF/close and resource counts.
After independent review, a bounded live series measures first versus subsequent
captures and verifies final cleanup and protected PIXIS/application state.
Physical exposure freshness remains unverified without controlled light changes.
