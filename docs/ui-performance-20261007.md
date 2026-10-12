# UI performance improvements — 2026-10-07

The saved Dual Gate configuration contains 49 batch rows (23 enabled) and
10,022 nested acquisition tasks. The history sample used for comparison has
25 Dual Gate runs and 117.7 MB of indexed settings. All profiling used Qt
offscreen widgets, disconnected controllers, and read-only SQLite connections.
No measurement was started and the running application was not restarted.

| Operation | Before | After |
| --- | ---: | ---: |
| Move batch row down / up, synchronous callback | 2.17 / 1.61 s | 0.40 / 0.13 s |
| Selected-row filename preview | 2.06 s | 0.059 s |
| Metadata edit | 1.21 s | 0.012 s |
| Voltage settling-time edit | 3.87 s | 0.00017 s |
| Active / completed frame update | 0.32–0.40 / 0.27–0.33 s | 0.0024 / 0.0031 s |
| Progress with full sequence visible / closed | 0.77–0.98 / 0.74–0.77 s | 0.0025 / 0.0014 s |
| Open full nested sequence | 1.80 s | 0.025 s to open; remaining items populate between Qt events |
| History preview text insertion | 8.30 s, 50.5 million characters | 0.0021 s, 23,685-character summary |
| Motion filename preview with simulated 300 ms SMU latency | 0.302 s | 0.00028 s |

These are local measurements, not latency guarantees. Filename preview retains
its existing 120 ms edit coalescing timer. The full sequence still retains every
task; independent review verified 10,001 tasks populated, with a maximum observed
5 ms Qt timer gap of 37 ms during population.

History list loading, settings search, preview and compatible-setting reads now
run away from the UI thread. For the measured record, background preview took
1.20 s and settings loading took 1.41 s. Their maximum Qt timer gaps were about
7 ms. Summary-page loading took 0.13 s and a full-settings no-match search took
1.10 s in the background. Large metadata JSON is decoded in a separate Python
process to avoid blocking GUI Python callbacks with the GIL. Cancellation
terminates obsolete helper processes, and only the latest request can update UI.

Dual Gate generates voltage vectors once per batch row, caches plan-derived
readiness requirements, and reuses its visible schedule for filename preview.
Settling-time changes update timing estimates. Instrument connection and health
checks remain current; a run still performs fresh safety validation. Nested
progress updates touch changed items and avoid serializing the full schedule.
Hidden full-sequence windows catch up when reopened. Large full trees populate
in chunks with Qt events processed between chunks.

Mega Sweep reuses voltage-grid data and plots for metadata, optical and timing
changes. Repeated preview of the 90,601-point stress grid took about 3 ms after
the first draw. Initial construction/drawing of a changed large grid still costs
about 3.2 s; the user's saved configuration has 121 points.

Motion filename previews use cached worker readbacks. Unknown optional channels
are omitted. Actual external-gate file names are refreshed from hardware on the
measurement worker before file creation. Pending gate targets keep their existing
filename semantics.

History summaries preserve full source files through **Open metadata**. Lists
query paged metadata fields rather than decoding all settings. Searches still
include complete settings and are no longer restricted to the first 500 records.
All 20 events on a page remain represented. Compatible-settings readers obtain
accepted keys from each adapter's existing setter table; unsupported key names
remain in the skipped report, without copying their large values back into the GUI.

Regression coverage includes ordering and draft behavior, safety invalidation,
timing and filename behavior, progress updates, chunked full-tree population,
history pagination and cancellation, large-record process isolation, adapter key
queries, optional SMU roles, and fresh worker readings for saved names. A 325-test
related suite passed, followed by focused checks for the final history refinements.

Comparison scripts and raw results are retained locally under
`tmp/ui-stall-audit-20261007`, `tmp/ui-stall-after-20261007`, and the corresponding
batch-row profile directories. These profiling outputs are intentionally ignored
by Git.

Restart the desktop app to load the UI and SMU-controller changes. The WinSpec
bridge does not need a restart for these changes.

## Dual Gate metadata finalization — 2026-10-08

The completed 10,022-task run registered 46 output files in its GUI completion
callback. Each registration rewrote the entire metadata JSON and history entry,
which prevented the Qt heartbeat from running and triggered an `UNRESPONSIVE`
notification before the eventual completion notification.

Output registration and terminal-state persistence now run after acquisition
cleanup in the existing worker, with one JSON write and one history update.
Completion notifications follow successful persistence. The UI shows saving
progress and refuses closing while the worker is active. Persistence failures
remain visible as failures, including after Stop; an identical registration can
retry a failed write.

New metadata keeps the full executed plan in `settings.requested`, but omits it
from `settings.loadable`, where it cannot be restored by a settings adapter.
Historical metadata remains unchanged. An isolated copy of the completed record
shrank from 55,583,361 to 28,575,058 bytes while retaining all 10,022 tasks. During
background finalization, the largest observed Qt timer gap was 67 ms; 46 outputs
and the terminal state were saved with one JSON write and one history update.
The save itself still takes time; these measurements verify UI responsiveness
and reduced duplication, not a guaranteed save duration.

The latest related regression run passed 97 tests and 2 subtests, including
metadata completion, cancellation, persistence retry, save failure after Stop,
GUI responsiveness and close protection. An earlier broader related run passed
192 tests and 30 subtests. Code review found no remaining important issues.
Tests used mock controllers and private temporary configuration/history files;
no live acquisition was started. Raw benchmark results are in
`tmp/metadata-finalization-20261008/real-metadata-benchmark.json`.

Restart SpectralSweep to load these changes. The running desktop application and
WinSpec bridge were not restarted during the investigation.
