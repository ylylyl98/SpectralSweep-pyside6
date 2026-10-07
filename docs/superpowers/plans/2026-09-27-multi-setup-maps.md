# Multi-setup 2D maps implementation plan

**Goal:** Run complete identical gate grids with PIXIS at 650 nm / 80 ms and WinSpec at 1050 nm / 250 ms, averaging four independent exposures per point.

**Architecture:** Extend each optical condition with a backend and reduction mode. A scan-only controller operation selects already connected sessions on the spectrometer worker thread without releasing measurement locks. A small adapter computes software means of single exposures; legacy conditions retain their existing EPF behavior.

**Constraints:** Do not interact with the running app or instruments. Tests use isolated configuration/data directories and simulated hardware. Keep user edits. Do not commit or change live configuration. Preserve raw Spectrum acquisition and temperature interlocks.

- [x] Add regression tests for condition persistence, four-frame averaging, cancellation and complete-map order.
- [x] Add scan session selection and preflight for all connected backends, with explicit failure propagation and UI notification after the run.
- [x] Extend the condition editor with setup and averaging controls; add the requested two-row recipe as an explicit UI action.
- [x] Resolve and validate sessions before gate writes; reacquire adapters per map; preserve zeroing and separate output files with setup/reduction metadata.
- [x] Run isolated regression tests and independent review; document usage and hardware verification limits.
