# Qt Quick performance measurements

Run from the repository with the bundled PySide environment. The native mode
uses a hidden Windows window and the software Quick renderer, matching the
workspace screenshot audit. It builds all nine real page classes through
`build_preview_window`; it never constructs MainWindow or connects devices.
Configuration, history and QSettings live in a temporary directory. Device and
notification imports are rejected, and config saves are blocked.

```powershell
.\.venv-pyside6-313\Scripts\python.exe tools/profile_quick_workspaces.py --native --repeat 3 --profile --output tmp/quick-performance/after.json
```

Each headline run is a fresh child process, run sequentially to avoid competing
with one another. `--profile` adds a separate process producing `.profile.txt`
and `.profile.pstats`; its instrumented timings are excluded from medians. Raw
per-process JSON and stdout/stderr logs remain alongside the combined report.
Do not run competing GUI tests while measuring. Compare the same scale, backend
and page fixture. A source fingerprint detects edits between repeated runs.

The report measures:

- Process entry to first synchronous workspace grab, page factory construction,
  and cumulative synchronous `QQuickWidget.setSource` time. The parent launch
  marker additionally includes Python process startup. OS caches are not flushed.
- Every page's first explicit visit, including lazy form migration, followed by
  three warm rounds. Routing time ends when `selectPage` returns. The second
  boundary includes two event-dispatch passes and grabs of all visible QML
  surfaces and the complete workspace. The initial Dual Gate page already exists
  at startup. These are synchronous rendering boundaries, not screen presentation
  timestamps or a promise that every queued background task has completed.
- A 21-character string typed by `QTest.keyClicks(delay=0)` into a visible real
  QML text editor. Its retained source `QLineEdit.textChanged` supplies the final
  character timestamp. The source string must match exactly. Rendering is timed
  separately, and the isolated fixture value is restored afterward.
- Five-second idle samples on Spectrum and Mega Sweep, after every page has
  been visited. Single-core equivalent CPU is process CPU seconds divided by
  elapsed wall seconds, times 100. Windows reports working-set RSS and peak RSS.
- A precise 10ms Qt timer records actual heartbeat intervals, percentiles, maxima
  and counts exceeding 50/100ms. Windows scheduler resolution can produce roughly
  15.6ms intervals. Idle sampling windows are never treated as response latency.
- Constructed/alive/visible surface counts and distinct native engine pointers,
  actual nonempty QML loads, source-clear count during teardown, and selected
  refresh/callback counts and elapsed times. Nested callback totals overlap.

The command fails on a callback exception, Qt warning, forbidden import, failed
input assertion, failed rendering, failed shutdown, or changing source between
headline runs. A clean performance report establishes the isolated UI behavior;
it does not exercise acquisition throughput or a hardware driver's latency.

The keyboard probe defaults to `--input-field laser_edit`, the exact field used
in the original baseline. Changing it changes the measured domain work, so such
input timings must not be presented as a before/after improvement.

`--backend software` is the default comparable benchmark. For a separate native
RHI smoke test, use `--native --backend default`: this removes QT_QUICK_BACKEND
from the child environment and records each visible view's actual graphics API.
Do not compare its timings directly with the software baseline.

```powershell
.\.venv-pyside6-313\Scripts\python.exe tools/profile_quick_workspaces.py --native --backend default --stream-test --output tmp/quick-performance/after-native-rhi.json
```

`--stream-test` optionally follows the comparable measurements with 60
precomputed synthetic spectra of 512 points at a requested 33ms timer cadence.
It calls the real Spectrum plot's `update_spectrum`, measures callback durations,
actual frame arrival intervals and heartbeat gaps, checks exact final raw/curve
arrays, and grabs the final rendered frame. No acquisition action is dispatched.
This exercises the UI publication path while the event loop remains active; it
does not measure a detector, disk writes or acquisition throughput. It is off by
default so the original baseline remains comparable.

## Recorded isolated Windows comparison

The retained `tmp/quick-performance/before.json` has one unprofiled baseline
process; `after.json` has three fresh sequential processes and reports medians.
All use software rendering, scale 1, the same nine-page fixture and `laser_edit`
input. The three final source fingerprints match
(`abf7bc43c7f9fcb7de57249ad8fa811527bc9ed472101052213419cfa807841e`).
The separate baseline profile and archived diagnostics under
`tmp/quick-performance/revision-c2f172ef3a/` identify hotspots without mixing
profiler overhead into these timings. The archived after-profile predates the
last palette/render-size fixes; it is not a profile of the final fingerprint.

| Measurement | Before | After |
| --- | ---: | ---: |
| Process entry through first workspace grab | 10.443 s | 5.041 s |
| Page factory construction | 9.057 s | 3.967 s |
| Startup synchronous QML loads | 1.813 s | 0.456 s |
| 21-character QML input through complete source string | 84.422 ms | 11.434 ms |
| Working-set RSS after visiting all pages | 769.1 MB | 485.4 MB |
| Idle CPU, Spectrum / Mega Sweep | 2.81% / 5.01% | 1.25% / 1.88% |
| Engines after visiting all pages | 106 | 1 |

Final first visits to pages other than the initial Dual Gate take 107–347 ms;
warm page switches including synchronous grabs take 10–24 ms. Both runs have
zero idle heartbeat intervals above 50 ms. Some first-use work remains, and the
single baseline process does not establish a statistical confidence interval.

`after-native-rhi.json` separately confirms Direct3D 11 rendering and 60/60 exact
synthetic frames at 30.21 fps. The 512-point update callback takes 0.712 ms P50,
1.098 ms P95 and 5.76 ms maximum; stream heartbeat P95 is 23.40 ms, maximum
24.39 ms, with no interval above 50 ms. Its final render/grab takes 29.75 ms.
The native backend has different resource costs (about 601 MB RSS and 6.89%
Mega Sweep idle CPU in this one run), so its timings and memory are not used in
the software comparison. Every final process closes cleanly without Qt warnings,
Python callback errors, forbidden imports or hardware actions.
