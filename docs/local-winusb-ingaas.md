# Local WinUSB InGaAs: experimental manual acquisition

The Windows 64-bit SpectralSweep app can acquire from the tested ST133/InGaAs
through a private 32-bit process. XP and the WinSpec GUI are not involved.
LightField continues to own the spectrograph and output route.

For moving the same detector/controller to another Windows 11 x64 PC, use the
[portable v6 installation guide](st133-new-pc-setup.md). The v5 instructions
below describe the original tested computer. A local v6 binding selects the
portable release without replacing that original installation.

The v5 release supports **integer 10–1000 ms exposures, 1–100 software
accumulations, and 512 × 1 raw uint16 subframes**. Accumulation sums in uint64
and displays the mean per exposure; exports retain every raw subframe and its
identity. Physical validation below covers accumulation counts 1, 2, 5 and 10;
the full 100-frame bound has not been exercised on hardware.
The first explicit Acquire 1D or Add spectrum starts a private helper
and initializes the camera once. Later requests at the same exposure reuse that
connection. A changed exposure normally closes and reinitializes the session;
it adds initialization time. The native exposure is read back before and after
initialization. Each
frame still requires its own START, STOP, USB drain, joined workers and verified
transport bytes before publication. Disconnect closes the native session and
verifies destruction and process exit. Empty idle sessions remain connected.
Applying settings does not start the camera. Each capture needs Apply (the
Spectrum buttons do this automatically).

Temperature is **unverified**. No Celsius conversion, lock status, cooling
control or passed temperature guard is claimed. The existing XP temperature
guard is unchanged. The UI and saved metadata identify this experimental mode.
Continuous acquisition, 2D frames, automated scans and wavelength
calibration are unavailable. Data uses pixel positions 1–512. Existing XP
calibrations are not automatically reassigned to the local detector identity.

## Installation on the tested computer

The persistent runtime is pinned in `app/devices/winusb_persistent_runtime.json`.
Every runtime file must match. Original vendor binaries remain outside Git.
On this tested computer the reviewed build is `tmp/persistent-builds/p011/runtime`:

```powershell
.venv-pyside6-313/Scripts/python.exe tools/winspec/install_local_winusb.py --persistent --source-runtime tmp/persistent-builds/p011/runtime
```

Installation copies verified files into
`.runtime/winusb-ingaas/runtime-v5` inside this checkout (ignored by Git). This
location is shared by normal and packaged launchers. It does not install a
driver, modify the source bundle or replace an existing installation. It opens
no device. This release is bound to the tested A010 controller, USB location
and controller/detector EEPROM identities; it is not a generic ST133 release.
It must not be pointed at PIXIS/A026.

Earlier runtimes, including v4, and the frozen diagnostic source tree are retained. Rebuilding
uses `tools/winspec/build_persistent_winusb.py --baseline-root <usb_compat>
--output <fresh directory>`. It requires the pinned original source/runtime
artifacts and local x86 compiler recorded by the builder. The build runs only
fake-transport tests and compiles, but does not execute, the physical helper.
Each rebuilt candidate needs a matching reviewed manifest before installation.

## Use

1. Release the ST133 USB device from the XP VM so Windows owns it with WinUSB.
2. Start the updated SpectralSweep app. Select the InGaAs + LightField setup.
3. In the connection settings, choose **Local WinUSB — manual acquisition
   (experimental)** under **InGaAs acquisition**. The XP address is disabled.
4. Connect the required devices and load the correct spectrograph experiment
   in the existing LightField session. Check the physical InGaAs output route.
5. In Spectrum, select the center wavelength, exposure and accumulation count,
   then **Acquire 1D**. Use **Save current…** to export the mean spectrum, all raw
   subframes and their metadata. The initial recipe remains 100 ms / 1 exposure.
   Legacy helpers continue to expose only their fixed 100 ms / 1-frame controls.

The default acquisition backend remains XP WinSpec; selecting Local WinUSB is
explicit. Source changes do not update an already-running Python process.
Finish any active measurement before restarting the app to load this release.

## Cancellation and failures

Abort cancels publication of the current spectrum, including the entire pending
accumulation. No partial average is published, and unacknowledged subframes
remain protected. The persistent helper completes
its own native STOP/drain and the bridge requests session close; Abort is not
an immediate hardware Stop. Reconnect explicitly after cancellation or failure.
The process is never killed and acquisition is never retried automatically.
On Windows, the reader opens published JSON receipts with read/delete sharing,
so an outstanding rename handle does not prevent reading the complete file.
It does not share write access, and rejects non-object or oversized (>16 KiB)
JSON receipts. If another lock makes a receipt temporarily unreadable with `PermissionError`, the
bridge waits for that same file under its original 30-second deadline and checks
for helper exit. It never resends START or extends the deadline. Invalid JSON
and other read errors fail immediately. Metadata records cumulative receipt-read
contention attempts for the session.

Native evidence is initially retained under
`.runtime/winusb-ingaas/sessions/` inside this checkout. Each session has a fresh
directory and each request has an immutable frame directory. Save-confirmed
retention (described below) can later remove eligible old temporary evidence. CSV metadata
includes those paths, payload/helper hashes, request/session IDs, sequence, PID,
cold-start status, timing and the explicit unverified temperature policy.
Per-frame cleanup is checked before publication; final session cleanup is
recorded only on close. Raw initialization/temperature evidence stays in the
native session files. Malformed, incomplete or cancelled frames are rejected.

Every request also records a UUID, bridge session and sequence, requested center
wavelength/settings, observed optics before capture, native process PID and
timestamps. The backend rejects a result with a different request ID or payload
hash. Responses are published atomically after their data files are complete. These
checks associate the software request with its returned bytes; they do **not**
prove that a complete frame came from the latest physical exposure. Metadata
therefore explicitly reports `hardware_frame_freshness_verified: false`.
Identical payloads are not automatically rejected: real dark or flat frames can
legitimately match. A controlled dark/bright alternation is still needed to test
physical frame freshness.

If the helper does not exit within 30 seconds, the application reports
unresolved ownership and retains the process. `active.json` prevents another
instance from starting a new capture. Inspect its output directory and
`process.json` to diagnose the retained owner. Do not remove the receipt or
terminate the helper while ownership is unresolved. Recovery requires checking
the native process and its cleanup evidence; automatic recovery is not provided
in this release. Failed disconnect retains the connection object so cleanup can
be retried; it does not report a successful disconnection.

## Validation scope

The persistent executable is a separately built release using the same fixed
native recipe as the October 13 XP/Win11 comparison. Its reusable frame/session
lifecycle and observer are separately tested. Original single-frame diagnostic
manifests and receipts remain unchanged.

Automated tests cover raw-data and lifecycle rejection, cancellation, owner
retention, fixed settings, scan restrictions, backend selection, and pixel/CSV
provenance. Temperature control, arbitrary exposures, hardware-triggered abort,
continuous acquisition and long scans require further native validation.

Validation on the tested computer, October 13, 2026:

- Relevant regression suite: 433 passed, 4 skipped.
- One live capture through the installed local bridge returned 512 uint16
  samples, including 113 zero values. Native/USB bytes and exported CSV values
  matched exactly. Cleanup and process exit were verified; no active owner
  receipt remained.
- Driver/configuration snapshots, PIXIS and existing measurement processes
  remained unchanged across that capture.
- The real frame was displayed and saved with SpectrumPanel. The normal
  controller Apply/Acquire flow was tested offline with an optics substitute;
  this smoke test did not command the live LightField optics or restart the
  existing measurement application.

Local evidence: `tmp/winspec-driver-rollback-20261013/app-integration-smoke/`.

## Per-frame latency

Before persistent sessions, the helper started and closed a full native camera
session for each frame. The app smoke took 2.03 seconds inside the helper: about 0.84 seconds
reading EEPROM, 0.56 seconds configuring/initializing, and 0.33 seconds from
Start to publication (including the 100 ms exposure). Those timings are measured
for this diagnostic lifecycle, not a limit on WinUSB throughput. An XP WinSpec
per-frame timing benchmark under matched settings has not yet been recorded.

The original app preflight launched PowerShell for each PnP query (about 2.1
seconds per call), at both Apply and Capture. It now queries current PnP metadata
directly with Windows Configuration Manager; it does not cache device presence,
open a camera handle or send camera commands. Each check requires the exact
controller instance, WinUSB service, a started devnode, and no problem/removal
flags. Driver/runtime checks and the native cleanup barriers remain enabled.
The implementation follows Microsoft's documented
[device lookup](https://learn.microsoft.com/en-us/windows/win32/api/cfgmgr32/nf-cfgmgr32-cm_locate_devnodew),
[service property](https://learn.microsoft.com/en-us/windows/win32/api/cfgmgr32/nf-cfgmgr32-cm_get_devnode_registry_propertyw), and
[status APIs](https://learn.microsoft.com/en-us/windows/win32/api/cfgmgr32/nf-cfgmgr32-cm_get_devnode_status).

After this change, six additional serial 100 ms hardware captures passed:

- PnP query median: **0.384 ms** over 20 read-only calls.
- Apply-equivalent preflight plus complete bridge capture: **2.383–2.804 s**,
  median **2.485 s**. This excludes physical LightField preparation and GUI
  updates; the older approximately 6.4 s figure was a component-based estimate,
  not a measured before/after GUI comparison.
- All six requests, output directories and payload hashes were distinct;
  native/USB bytes and cleanup evidence passed. No active owner remained.
- Protected drivers, configuration, PIXIS and existing measurement processes
  were unchanged. Illumination was uncontrolled and optics were not commanded,
  so this does not establish absence of a full stale physical frame.
- Updated relevant regression suite: **450 passed, 4 skipped**.

Evidence: `tmp/winspec-driver-rollback-20261013/latency-validation-01/`.

The persistent release now performs identity/configuration once per connection.
Offline validation includes multiple changing frames, consumer-unlock/rearm
races, short/stale/error/disconnected transfers, late native errors, atomic
receipt publication, EOF/truncated commands, cancellation and retained ownership.
Physical exposure freshness and long-duration continuous acquisition still need
separate validation before enabling continuous acquisition.

### Persistent-session measurement

Six explicit physical captures on October 13, 2026 (local time), all at 100 ms:

- First frame including initialization: **2.358 s**.
- Next five frames: **0.216–0.252 s**, median **0.224 s**.
- Compared with the previous separate-process median of 2.485 s, subsequent
  bridge captures took about **91% less time** (approximately 11.1 times faster).
  Both series include an Apply-equivalent bridge preflight and exclude physical
  LightField preparation and GUI updates. These are separate short series, not
  a matched XP benchmark or a continuous-acquisition throughput specification.
- All six frames used one helper PID; identity and configure each ran once;
  six START requests completed with distinct UUIDs and payload hashes. Each
  1024-byte native frame matched its observed USB transfer. Final close verified
  all six frames and error zero; the helper exited and released the owner claim.
- Zero counts remained 114, 112, 113, 113, 114, 112. Persistent initialization
  addresses latency; it does not resolve the previously observed zero pixels.
- Protected drivers, configuration, PIXIS and running measurement processes
  remained unchanged. No physical LightField optics commands were issued.
- Relevant WinUSB/WinSpec/LightField/controller/UI regression suite:
  **555 passed, 4 skipped**, including the actual helper with fake transport.

The historical p007/v2 helper SHA-256 is
`4481a67074808fb11576ae95e784ac7565cdfa9102290727782f9a90c482f3dd`;
its offline build evidence SHA-256 is
`dd69ed1bfc3889066e91bee0ff30e7bce907c45dc2896df9496ca0ae7d831ef9`.
Build evidence: `tmp/persistent-builds/p007/evidence.json`.
Hardware evidence:
`tmp/winspec-driver-rollback-20261013/persistent-validation-01/`.
Restart SpectralSweep after the current measurement finishes to load the code;
the already-running application was not restarted during this validation.

### Optical validation preparation

`tools/winspec/validate_local_optical.py` adds a diagnostic around the unchanged
persistent bridge. It keeps the same session across explicitly confirmed dark
and light batches, saves every frame including the first after each transition,
and stops on the first timing, identity, clipping or optical-state failure.
The initial reference needs 20 dark and 20 light frames. Its predefined statistic
is the mean across all 512 pixels, including zero pixels. Light-minus-dark must
exceed both 5 mean ADU and five times the larger within-state standard deviation;
no full-scale samples are accepted. A failed separation gate is inconclusive
about old frames, not proof of a driver fault. Later frames are compared with
these frozen references; signal drift can also cause rejection.

The native timing gate uses request-scoped `start_call_before` and
`publication_observed` log events, with a 95 ms diagnostic minimum for the fixed
100 ms recipe. It does not pad early results, measure the physical exposure gate,
or prove freshness. Labels record the operator's reported illumination rather
than an independently measured shutter state. Temperature remains unverified.
Repeated nonperiodic light changes and return-to-dark checks are required before
drawing optical conclusions. The private lab runner caps a session at 160 frames,
accepts at most 20 per explicit command, and closes normally after ten idle
minutes. No camera is opened by importing the diagnostic or running its tests.

Preparation checks: 18 focused tests passed, one optional native-fake integration
test skipped. Read-only protected-state/runtime/device preflight passed. Physical
optical capture was not started. The operator cannot block the light and
requested direct testing, so the unchanged-illumination stability diagnostic
below is used instead. Evidence: `tmp/winspec-driver-rollback-20261013/optical-preflight-01.json`.

### Extended stability validation and fixes

The original p007 event registry counted every event ever created toward a
32-slot limit, even after events were closed. A long fake-transport test
reproduced failure on request 14 (13 completed frames), with verified cleanup.
The separately built p008/v3 release reuses freed slots while retaining a limit
of 32 live events, preserves tracking on failed close, and requires both zero
live events and balanced lifetime counts at destruction. The frozen source
tree is unchanged; the builder makes a hash-checked private source copy.

Native offline checks passed 1,000 frames with one initialization, 2,006 events
closed, and identical initial/final handle counts (238). A separate real-Windows
event test checked the live limit, failed-close retention, slot reuse and 2,033
balanced event creations/closes. The p008 helper SHA-256 is
`7f6f969139bcade8244ebdad6e452dbe9f494de4cdb62ec9fe42ff7c16cd62bd`;
build evidence SHA-256 is
`0ce6d0b94fb09696c619b1597aa8569d0a45a45a3e923901f4071d9bd657ce53`.
Build evidence: `tmp/persistent-builds/p008/evidence.json`.

The first physical stability attempt stopped after 23 accepted frames because
Python received `PermissionError` when opening frame 24's receipt. Native frame
24 was complete: 1,024 bytes matched the USB transfer, its request identifiers
matched, and its START-to-publication duration was 107.624 ms. Normal close
reported 24 completed frames and error zero; protected snapshots were unchanged.
Evidence: `tmp/winspec-driver-rollback-20261013/stability-validation-01/`.

A real Windows DELETE-access file handle reproduced the same Python errno 13
when reading an existing JSON file. Windows requires compatible sharing modes
until the conflicting handle closes, including sharing delete access for rename
handles ([Microsoft CreateFileW documentation](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)).
This supports a transient sharing-conflict explanation; the actual handle holder
in the physical failure was not identified. The original traceback was observed
in runner output; its saved result contains only the wrapper error. The updated
runner saves full failure tracebacks. Regression tests verify recovery after
releasing a real file lock, bounded failure with a persistent lock, and immediate
rejection of malformed JSON. Receipt waiting does not resend acquisition or
change the fixed recipe; it can add host-side delay before the next request.

`tools/winspec/validate_local_stability.py` saves every frame, checks native
identity, byte equality, request order, cleanup and request-scoped timing, and
stops on the first failure. The lab runner checks 100 frames before continuing
to a total of 1,000 in the same session, with mixed delays and first-frame checks
after 5- and 30-second idle periods. Resource counters and protected-state
snapshots are recorded. Illumination is unchanged/uncontrolled; physical frame
freshness, physical exposure duration and temperature remain unverified.

The second physical run on October 13, 2026 (local time) completed all **1,000
frames** in one session, with identity/configuration each performed once:

- All 1,000 request UUIDs and payload hashes were distinct. Each 1,024-byte
  frame matched its USB transport and separately saved diagnostic copy. No
  incomplete, full-scale, all-zero or mismatched-request frame was accepted.
- First bridge capture: **2.501 s**. Subsequent captures: median **0.272 s**,
  95th percentile **0.318 s**, range **0.199–1.075 s**. These exclude GUI and
  live LightField preparation. Occasional latency spikes remain; for example,
  frame 423 spent 697.769 ms inside the native START call. The cause of those
  longer calls was not established.
- START-call entry to publication: median **108.8075 ms**, minimum **104.982 ms**,
  maximum **792.295 ms**. No request violated the predefined 95 ms minimum.
  This interval includes native-call overhead and does not directly measure the
  physical exposure. START-call-return to publication can be shorter than
  100 ms, because exposure may already have begun inside the call.
- The first frames after 5 s and 30 s planned idle periods had mean signals
  within **+0.048%** and **-0.070%** of their neighboring-frame medians. Their
  START-to-publication intervals were 110.202 ms and 149.241 ms. This is an
  unchanged-illumination comparison, not an optical freshness test.
- Six receipt-read contention attempts occurred across six frames and recovered
  by reading the same file. There were exactly 1,000 native START requests;
  none of these reads caused an additional capture.
- Sampled helper handle count stayed **267** from frame 20 through frame 1,000.
  Private memory increased by 110,592 bytes (108 KiB) between those samples;
  thread counts were 1–4. Final destruction closed **2,006 events**, reported
  1,000 completed frames/error zero, and the process exited normally. The owner
  claim was released. This bounded series does not establish indefinite uptime.
- Every frame still contained **110–116 zero pixels**. The test does not explain
  their cause or certify temperature. No optics, cooling or driver changes were
  made; protected drivers/configuration/PIXIS/measurement snapshots matched.
- Final relevant regression suite: **573 passed, 4 skipped**, with the p008
  offline native helper enabled. The real Windows receipt-lock regression and
  independent saved-evidence revalidation passed.

Evidence: `tmp/winspec-driver-rollback-20261013/stability-validation-02/`, including
`result.json`, `saved-evidence-analysis.json`, every raw frame and protected
snapshots. Native evidence:
`.runtime/winusb-ingaas/sessions/20261014T030332-91ab0f45/native/`.
The previously running SpectralSweep application was not restarted; load these
changes after its current measurement has finished. Passing this series supports
bounded software/transport stability. It does not rule out a complete old
physical frame with valid bytes, certify the exposure gate, or replace optical
and temperature validation before broader measurement modes are enabled.

### Receipt handoff revision

The Python receipt reader now explicitly uses `CreateFileW` with
`GENERIC_READ`, `FILE_SHARE_READ | FILE_SHARE_DELETE`, and `OPEN_EXISTING`.
The native writer already finishes, flushes and closes the temporary file
before atomic rename. Sharing delete access lets a reader open that fully
written file while a rename handle is still held. Write sharing is omitted so
an outstanding writer must close before the receipt is accepted. This follows
the Windows sharing contract linked above; the holder of the original physical
run's temporary lock remains unidentified.

This change covers ready, frame and close receipts through the common reader.
The bounded wait remains for other transient locks, with the same original
deadline and no acquisition retry. Only a UTF-8 JSON object of at most 16 KiB
is accepted; request/session/sequence, frame bytes and native cleanup validation
are unchanged. Native p008/v3 binaries and their acquisition recipe are unchanged.

The reader transfers each Windows handle to a CRT descriptor, then keeps that
descriptor explicitly owned while using a Python stream with `closefd=False`.
A context-manager `finally` closes it once. Review found and regression tests
reproduced the alternative's double-close risk when stream construction had
already consumed a descriptor before raising. Ownership now follows the
[CRT handle-transfer contract](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-osfhandle).

Offline tests use a real DELETE-access handle held across an actual file rename:
ordinary `Path.read_text` fails, while the new bridge reads the same receipt
without waiting or retrying. Other tests cover a still-open writer, exclusive
locks released later or held through the timeout, invalid JSON/size/shape,
1,000 reads without handle growth, and failures at both ownership-transfer
boundaries. The file lock tests do not open or command a camera.

Physical revalidation with the new reader completed another **1,000 frames**
at fixed 100 ms with unchanged illumination, one native session, and the same
mixed-idle schedule. This run recorded **zero receipt-read contention attempts**
(the preceding run with the waiting-only reader recorded six). All 1,000 raw
frames, USB snapshots, saved copies, request identifiers and receipts passed
independent revalidation. No acquisition was retried. The two runs are separate
series, not proof that every possible permission error has been eliminated.

First bridge capture was 2.486 s; subsequent captures had median **0.272 s**,
95th percentile **0.318 s**, and range **0.202–0.692 s**. Native START entry to
publication ranged 105.807–502.947 ms. First-frame checks after 5 s and 30 s idle
passed. Helper handle samples stayed at 267; Python-reader spot checks after
frames 425 and 900 both showed 167 handles. Normal close reported 1,000 frames,
error zero and 2,006 closed events. The owner claim and processes were released,
and protected-state snapshots matched. Zero pixels remained 110–117 per frame;
temperature, physical exposure duration and physical frame freshness remain
unverified.

Evidence: `tmp/winspec-driver-rollback-20261013/stability-validation-03/`.
Native session: `.runtime/winusb-ingaas/sessions/20261014T033439-e5f4186c/native/`.
The runner's implementation pins include `app/devices/winusb_evidence.py`.
Final relevant regression suite: **584 passed, 4 skipped**, including native
fake transport and 14 receipt-reader tests. One earlier broad run hit the
unchanged LightField 20 ms frozen-setting test's timing-sensitive error-message
assertion; its isolated 16-test file and the full final rerun passed. No
LightField source or test was changed to obtain that result.

The requested broader exposure, accumulation, stop/reconnect and temperature
qualification remains a separate capability stage. This receipt-handoff change
does not enable those modes or remove the fixed-100-ms restriction.

### Save-confirmed cache retention (2026-10-14)

`app/devices/winusb_retention.py` adds retention to the existing persistent
bridge and SpectrumPanel save path. It does not change the native executable,
driver, exposure recipe, frame validation or acquisition retry policy.

After CSV and completed experiment metadata are saved, the panel acknowledges
the **frozen acquisition snapshot**, not the controller's current frame. The
acknowledgement verifies the exported file hash, completed/non-degraded metadata,
session/request/sequence and the SHA-256 reconstructed from all 512 saved raw
uint16 counts. It flushes the output files before committing an acknowledgement
to `.runtime/winusb-ingaas/retention.sqlite3`. Saving an experiment inside the
temporary cache cannot authorize cleanup. Missing or failed acknowledgement
keeps the native files and logs a warning; it does not retry acquisition.

Cleanup runs on the acquisition worker between explicit requests (under its
existing acquisition lock), and after verified normal native close. A save
after disconnect is acknowledged immediately and becomes eligible on a later
capture/close cleanup pass. The saver itself does not delete files. Each pass
processes at most 32 frame candidates and 32 session candidates; locked or changed
items are skipped and the cursor moves on. Cleanup rechecks exported hashes,
raw counts and the remaining native file identities before removing only the
five known files for that frame. It never recursively deletes a frame directory
or follows a symlink/junction.

Defaults (configurable before connection):

| Setting | Default | Meaning |
| --- | ---: | --- |
| `keep_frames` | 1000 | Protect the newest 1000 indexed frames, including unsaved/failed frames |
| `max_frames` | 10000 | Admission threshold for all persistent and historical cached frame directories |
| `max_files` | 60000 | Admission threshold for cached files |
| `max_bytes` | 1073741824 | 1 GiB estimated cache allocation, rounding small files up to 4 KiB |
| `min_free_bytes` | 536870912 | Keep at least 512 MiB disk headroom |
| `batch_size` | 32 | Maximum frame/log-segment/session candidates in each cleanup category |
| `keep_sessions` | 10 | Keep headers, initialization log and final log of the newest 10 successful closed sessions |
| `max_session_log_bytes` | 67108864 | v4: rotate the log at the next frame boundary after 64 MiB; legacy helpers: admission limit |

Override individual settings in
`.runtime/winusb-ingaas/retention-policy.json`, for example:

```json
{"keep_frames": 1000, "max_frames": 10000, "max_bytes": 1073741824}
```

Policy values must be positive integers (`min_free_bytes` may be zero), and
`keep_frames` must be smaller than `max_frames`. No policy override file is
created by default. Thresholds are checked **before** sending the next native
request, with an additional 1 MiB/16-file reserve. A just-completed frame can
temporarily bring the recent-frame window to 1001 until the next cleanup pass.
An invalid policy/index, incomplete inventory, insufficient space or exceeded
threshold prevents a new request. Capacity pressure leaves the existing helper
idle and normally closable; it does not kill/restart it or resend a capture.

The active native log is never truncated or renamed. The v4 runtime rotates it
between complete frame requests, as described below. Older successful closed
session headers/logs are removed only after every indexed frame has been safely
reclaimed, close proof is valid, no unexpected files remain, and the recent-session
window allows it. Legacy helpers without the log protocol still stop admitting
requests at their session-log limit; they require an explicit normal reconnection.

Unsaved, incomplete, failed and ownership-unresolved sessions remain protected.
Their retained contents can exhaust the global budget; new acquisition then
stops rather than evicting scientific data. Historical runs predating this
index, including the original `captures/` directory, count toward capacity but
are never adopted for deletion. Existing physical validation evidence is kept.
Both full inventory and directory-read failures fail closed; partial counts are
never reused on retry. Inventory runs once per cache owner and is maintained
incrementally; a pressure-triggered rescan recognizes evidence the user has
archived while idle. Installed runtime binaries and permanent experiment output
are outside the cleanup scope.

Validation uses temporary directories and fake transport only. A real p008
native executable in `--offline-session` mode completed 60 consecutive explicit
requests while each result was exported and acknowledged. With test settings
`keep_frames=5`, `max_frames=20`, cache stayed at most 6 frames and ended at 5,
using one initialized process with sequences 1–60 and normal close proof. Tests
also cover actual SpectrumPanel saves, failed metadata writes, restart recovery,
changed exports/native files, Windows junctions, locked-file retries, failed and
unfinished frames, stale counts after archival, historical usage, each capacity
gate and a corrupt index that blocks capture while permitting orderly close.
This cache revision has not yet been exercised on the physical camera. The
earlier 1000-frame physical runs tested the preceding receipt-reader revision.

Final verification for this revision: **57 targeted tests passed**, including the
60-frame native fake-transport run and a real Windows junction test. The broader
WinUSB/WinSpec/LightField/controller/spectrum/experiment regression selection
completed **677 passed, 4 skipped** in 92.60 seconds. The pinned installed v3
runtime hashes and the original comparison/UI patch preservation check passed.
The running measurement processes were not restarted, and no retention index or
cleanup was created in the real camera cache during validation.

### Persistent native log rotation (v4, 2026-10-14)

The v4 helper keeps the camera connection and controller initialization while
rotating the native log. Before the next explicit capture request, after the
previous frame's STOP/drain/worker-join checks, it creates the next numbered log,
flushes and closes the previous file, publishes its immutable sealed receipt,
then atomically replaces `log-state.json`. A native writer lock covers both
writes and rotation. No camera reconnect, reset, automatic capture retry or
additional initialization is part of rotation. File flushing can add a small
delay between frames; the threshold is soft because an entire frame remains in
one segment. The configured threshold must be 4 KiB through 512 MiB.

`ready.json` advertises `log_protocol: 1` and the configured threshold. Each
`frame.json` identifies its `log_segment`; a seal records session identity,
segment number, completed frame range, final byte size and error state. The
Python bridge validates the handshake before sending any capture, and links
each validated frame to the current indexed segment. Optical and stability
diagnostics follow that segment instead of always reading `session.jsonl`.

Cleanup only reclaims a sealed middle segment after **every dependent frame**
has been exported, acknowledged and reclaimed outside the recent-frame window.
It verifies indexed hashes before unlinking the two known log/seal files. The
initialization segment, active/final segment, recent or unsaved frames, failed
sessions and incomplete evidence remain protected. Interrupted log/seal deletion
can resume after rebuilding the cache owner. The old per-record stdout mirror
is removed, so `stdout.txt` cannot silently duplicate the growing acquisition log.

Failure to create, flush, seal or publish a log segment stops admission before
the next exposure. A logging fault immediately before START is checked again
before issuing START. Normal cleanup is attempted, evidence is retained, and
there is no retry. Actual disk exhaustion, unsaved data filling the global cache,
invalid evidence or the existing 999999-request limit still stop acquisition;
rotation only removes the former single-log-size pause.

The update requires a new application process to load the Python changes.
Apply it by normally closing and reopening SpectralSweep after the current
measurement finishes. Existing measurement processes are not restarted by the
installation, and the v3 runtime remains available alongside v4.

Verification for this revision:

- `tmp/persistent-builds/p010/evidence.json`: native build, 1000 sequential fake
  transport frames, EOF/partial-command/ready failure tests and modes
  13/16/18/19/20/21/22/23/24 passed. Physical helper was compiled, never executed.
- Eight dedicated rotation tests passed: 80 frames across at least 20 log
  segments, one initialization, complete frame/segment association, balanced
  process handles and bounded stdout; 36 bridge captures with real exports and
  bounded cache; injected pre-START log failure; blocked next-log, seal and state
  publication all produced no extra START and preserved prior frame bytes.
- Broader application regression: **699 passed, 4 skipped**, 126.48 s, exit 0.
  This includes invalid handshakes, unsaved/failed evidence protection, segment
  deletion/restart recovery and normal closed-session reclamation.
- Independent review has no remaining actionable findings. Installed v4 pins
  verify, v3 remains present, original comparison/UI patch reverse-check passes.
  Existing measurement PIDs 24376/54284 retain their original start times.
  No real native ownership claim or retention index was created.

The physical helper SHA-256 is
`8ee9866579e6e90c2b1bd22d93ada9dd9e1a76dfe13b699fdb653a26641bb86e`;
the build evidence SHA-256 is
`293b8cf544ea8c5155ab9d39c1803e4a825f36fb2a8d01523439eefd9951f592`.
The installed manifest pins both. These are offline results, not a physical
camera endurance qualification. Exposure remains fixed at 100 ms, one exposure
per request; cooling, temperature and hardware frame freshness remain unverified.

### Physical v4 rotation and save-retention test (2026-10-14)

The installed v4 helper completed **1000 physical A010/ST133 frames** in one
connected session. The test used the production bridge and experiment save/
acknowledgement functions, fixed 100 ms exposure, unchanged/uncontrolled light,
and no temperature or optics writes. It did not operate the existing GUI or an
automated wavelength scan. Runtime policy overrides were confined to this test
process: a 16 KiB rotation threshold and a 50-frame recent window, so rotation
and reclamation occurred repeatedly. The configured application defaults remain
64 MiB and 1000 recent frames.

Before physical acquisition, an offline rehearsal exposed an eligible-frame
starvation bug: a frame saved after the cleanup cursor had passed it could wait
indefinitely while newer saved frames kept arriving. Cleanup now freezes an
upper ID for each sweep round, including log and session sweeps. New arrivals
cannot keep an old round open. Regression tests cover continuous arrivals,
late saves with batch sizes 1/4, and formerly locked log segments. **71 scoped
tests passed, 2 native tests skipped**; independent review found no outstanding
issue. A 120-frame fake-transport rehearsal then verified delayed save and
cleanup across 21 segments. Its initial final-check failure was a test assertion
expecting absolute paths in portable export metadata; corrected read-only
revalidation is recorded separately, preserving the original test result.
The native v4 binary and its pinned build evidence were unchanged.

Physical evidence: `tmp/v4-rotation-physical-01/result.json`,
`post-analysis.json`, `protected-before.json`, `protected-after.json`, and
the per-frame exports/receipts/raw copies and archived sealed logs beneath that
directory. The active session was
`.runtime/winusb-ingaas/sessions/20261014T050538-0f6ad6c4`.

| Check | Observed result |
| --- | --- |
| Frames / elapsed time including export, checks and planned waits | 1000 / 1631.56 s (27.19 min) |
| Native identity / configuration / destruction stages | 1 / 1 / 1 |
| Sequence, request IDs and payload hashes | Sequences 1–1000; 1000 unique IDs and hashes; no exact duplicate payload |
| Completed exports and saved evidence verification | 1000; all verified |
| Receipt read contentions / native close error | 0 / 0 |
| Sealed log segments | 169, segments 0–168; contiguous request coverage verified |
| Live cache peak / after normal close | 52 frames / 50 frames |
| Live log peak / after normal close | 12 log segments / 10 segments |
| Delayed save | Frame 11 protected through frame 100, then exported, acknowledged and reclaimed during continuing acquisition |
| Helper handles in 11 samples | 267 throughout |
| Sampled native private memory | 8945664–9273344 bytes; no sustained growth |
| Protected drivers, configuration and pre-existing measurement processes | Unchanged; test helper exited normally and owner claim was released |

The retained test exports and archived log copies are deliberately outside the
temporary cache, so cache deletion does not remove the validation evidence.
The session's maximum sampled logical file size was 469266 bytes, which is not
a measurement of filesystem allocation.

Warm bridge calls had median **0.714 s**, p95 **0.943 s**, maximum **2.167 s**.
Calls without rotation had median 0.702 s; calls with rotation had median
**0.866 s**. This confirms that rotation retains the connection but has measurable
work between frames; it does not establish constant frame cadence. Native START
to publication had median **108.876 ms**, p95 121.767 ms, and maximum 545.523 ms.
These are software observation intervals, not calibrated physical exposure
durations. The earlier v3 median 0.272 s was measured without this per-frame
save/reclaim stress workload and cannot isolate a rotation performance change.

After 5 s and 30 s planned idle periods, first-frame checks passed, with native
START-to-publication intervals of 109.118 and 110.076 ms. Their mean signals were
0.99938 and 0.99854 of neighboring-frame medians, respectively.

Zero readings remain: each frame contained **107–116 zero-valued pixels**, and
**107 pixel positions were zero in all 1000 frames**. No all-zero frame or
full-scale sample was seen. This run does not establish the cause of those
zeros, validate temperature/cooling, or independently prove optical frame
freshness under controlled illumination. The evidence supports the bounded
transport, receipt, rotation and retention checks; it is not a day-scale
endurance or general exposure/accumulation qualification.

### Offline pixel analysis of the v4 physical run (2026-10-14)

`tmp/analyze_v4_saved_pixels.py` re-read all 1000 preserved raw frames without
operating the camera. Each 1024-byte payload was checked against its saved
SHA-256, sequence, session, native receipt and recorded statistics before
decoding as 512 little-endian unsigned 16-bit samples. The recomputed per-pixel
zero frequencies also match the earlier post-analysis. All pixel numbers below
are **1-based**. No background subtraction, interpolation or zero replacement
was applied.

| Pixel analysis | Result |
| --- | --- |
| Zero in all 1000 frames | 107 / 512 pixels (20.90%) |
| Sometimes zero | 11 pixels |
| Never zero in this run | 394 pixels |
| Longest consecutive always-zero group | 2 pixels; positions are dispersed across the array |
| Always-zero counts in successive 64-pixel blocks | 13, 17, 11, 9, 14, 15, 12, 16 |
| Mean across first / last 100 frames | 2758.138 / 2815.953 counts, a 2.096% increase |
| Entirely zero frames / full-scale samples | 0 / 0 |

The intermittent positions are 35, 70, 148, 198, 205, 358, 361, 390, 447, 464
and 506. Temporal standard deviations include drift and changes in uncontrolled
illumination; they are not isolated detector read-noise estimates. The cause
of the slow mean change is not established.

The three valid historical XP frames (`xp-002` through `xp-004` in
`tmp/winspec-driver-rollback-20261013`) and three prior Win11 frames were
revalidated with their existing read-only evidence parsers, including original
SPE hashes, headers and acquisition-completion records. The failed/cancelled
XP overrun frame `xp-001` remains excluded. **All 107 always-zero v4 positions
were also zero in all three valid XP frames and all three prior Win11 frames.**
XP has 111 positions zero in all its three frames; its four additional positions
(70, 198, 447 and 464) are intermittent in the longer v4 run, zero in 943, 883,
160 and 937 of the 1000 frames respectively. Different sample counts and
conditions prevent interpreting this as recovery of four pixels.

The XP/v4 mean-spectrum Pearson correlation is 0.999938 across all pixels,
and 0.999907 after excluding positions always zero in either set. The overall
mean ratio is 1.001440. These describe similar saved patterns, not photometric
equivalence: gain/shutter differences remain, acquisitions were about six hours
apart, illumination was unmeasured and physical temperature was not verified.

For eligible adjacent frames (excluding the cold-start pair and planned-idle
pairs), mean-count changes had median +0.076 counts across 149 rotation
boundaries and -0.004 counts across 746 non-rotation boundaries. Median change
in zero count was 0 in both groups; their 5th/95th percentiles were -2/+2.
This descriptive comparison does not establish a causal absence of rotation
effects, but the fixed-zero pattern demonstrably predates v4.

Outputs are preserved in `tmp/v4-rotation-physical-01/pixel-analysis/`:
`summary.json`, `pixels.json` (all 512 pixel statistics), `frames.json`,
`source-hashes.json`, `pixel-overview.png` and `xp-comparison.png`.
The evidence does not support attributing the 107 fixed zeros to new v4 receipt
handoff or log rotation. It does not identify temperature, controller settings
or detector damage as their cause, nor establish that the pixels are dead.
Further diagnosis needs verified temperature and matched acquisition settings;
existing unvalidated temperature readings must not be treated as physical
temperature measurements.

### Variable recipes and measured latency (v5, 2026-10-14)

Installed release: `experimental-persistent-recipes-v5`, built in
`tmp/persistent-builds/p011`. Helper SHA-256:
`3f263c636b5ef604eb8372d9592fd98675ff6262881b6413794e7ee7c8e7445c`.
Build evidence SHA-256:
`df2203b888fb76259a917c5406744ceeb018ca3bebc131409e86bc67d76696a0`.
The frozen vendor sources and the v4 installation were retained.

The bridge negotiates recipe protocol 1. Exposure changes require verified
normal cleanup before another session may start. A nonzero close error leaves
the bridge faulted even when cleanup succeeded; it cannot become an implicit
retry. Each accumulated subframe retains its request UUID, consecutive sequence,
session, exposure, raw counts, payload hash and native receipt. A completed
export must match the entire frozen portable acquisition evidence and every raw
subframe before one transaction acknowledges all members for later cleanup.
Changed, missing or partially saved evidence authorizes no member.

`tmp/validate_winusb_recipes_live.py` exercised the production setup acquisition,
accumulation, bridge and export code with an explicitly unverified no-op optics
facade. It sent no LightField, routing, cooling or temperature commands. This is
detector validation, not an optical-routing or end-to-end scan qualification.
Evidence is in `tmp/v5-recipes-physical-01/`: 45 CSV/metadata pairs, `frames.json`,
`result.json`, complete `native-archive/`, the executed harness, protected-state
snapshots and independent `post-analysis.json`.

- **75 physical raw frames**, 75 distinct request IDs and payload hashes;
  73 frames formed **45 complete exported spectra**. Every exported CSV mean,
  constituent hash and archived receipt was independently rechecked.
- Five exposures, six frames per group, plus a return to 100 ms; all seven
  sessions passed exposure readback before/after initialization and normal close.
- At 100 ms, accumulation counts **2, 5 and 10**, two spectra each, passed exact
  sum/mean and constituent correspondence checks. Single-frame behavior also
  passed. These are sequential host accumulations, not native on-camera summation.
- Cancellation during the second pending frame of a requested five-frame batch
  produced no spectrum and no third request. Both raw frames remained, with no
  save acknowledgement. The helper drained and closed normally. An explicit
  new bridge then acquired and exported three frames successfully.
- Protected driver/configuration/process snapshots matched. The existing
  measurement processes were not restarted. Temperature and controlled optical
  frame freshness remain unverified; automated scans remain disabled.

| Exposure | Warm native START → publication median | Signal mean median | Full-scale pixels per frame |
| --- | ---: | ---: | ---: |
| 10 ms | 15.055 ms | 213.923 | 0 |
| 50 ms | 61.933 ms | 1454.970 | 0 |
| 100 ms, first group | 107.927 ms | 3001.888 | 0 |
| 300 ms | 312.930 ms | 8858.816 | 0 |
| 1000 ms | 1016.458 ms | 24102.109 | 14–16 |
| 100 ms, return group | 109.130 ms | 2998.148 | 0 |

Native intervals are software observations, not calibrated physical exposure
durations. The return-to-100-ms signal changed by −0.125%. Longer exposure
increased the signal, but 1000 ms saturated some pixels. The post-analysis also
reports responses excluding every pixel that reached full scale; this does not
qualify photometric linearity under uncontrolled illumination and unverified
temperature. Distinct payloads and correct host correspondence alone cannot
prove that every detector exposure is physically fresh.

The profiling harness `tmp/profile_winusb_stages.py` used 40 physical 100 ms
frames per run, CSV plus completed metadata and acknowledgement for every frame,
16 KiB log rotation and a five-frame retention window. Frames 2–10 provide the
unprofiled warm comparison below; frames 11–40 additionally ran cProfile. Both
baseline and final runs closed normally with 40 frames, no native error and
unchanged protected state. Source, runtime and cache state differ between runs;
these short measurements characterize this computer, not a latency guarantee.

| Unprofiled warm median | Original v4 / Python | Installed v5 / updated Python |
| --- | ---: | ---: |
| Read-only preflight | 0.00103 s | 0.00109 s |
| Cache admission / maintenance before request | 0.22515 s | 0.12072 s |
| Request receipt wait | 0.28698 s | 0.25443 s |
| Frame registration, now including deferred cleanup | 0.11772 s | 0.14782 s |
| Complete bridge capture call | **0.65584 s** | **0.56916 s** |
| CSV and durable experiment metadata | 0.41307 s | 0.43693 s |
| Save acknowledgement | 0.12431 s | 0.13342 s |
| Per-frame capture + export + acknowledgement | **1.26674 s** | **1.17314 s** |

Capture improved by about **13.2%** and the measured complete workflow by
**7.4%**. End-to-end medians are computed per frame, not by summing the displayed
stage medians. The earlier 0.714 s long-run result had different conditions and
is not the matched baseline. An intermediate v4/new-Python run measured 0.448 s,
but overlapped a broad test run and is not used as the final performance claim.

Profiling identified SQLite durable commits, export fsync and cache path/file
checks as substantial host costs. Cleanup of older saved frames now shares the
validated frame's registration transaction while the acquisition lock is held.
Capacity is still checked before every request; under pressure, cleanup and a
fresh inventory precede admission. Unsaved/active/failed evidence, full hash and
receipt checks, durability and normal cleanup rules remain in force. No database
durability setting, device check or native STOP/drain check was disabled.
Export/acknowledgement latency was not improved by this change. Evidence:
`tmp/v5-profile-before-01/`, `tmp/v5-profile-python-after-01/` and
`tmp/v5-profile-final-01/`, each with per-frame timings and cProfile output.

Validation after installation: **366 targeted Python/Qt tests passed** with
21 opt-in native tests skipped in that invocation. The separate p011 native
recipe/lifecycle/rotation invocation passed **39 tests**, including those
opt-in cases, using fake USB only. A broader full-repository run was deliberately
stopped before completion to avoid background load; no full-suite pass is claimed.
Independent review findings on close errors and rewritten saved evidence were
reproduced with failing tests and fixed before physical execution.

### Host export and cache measurement optimization (2026-10-14)

The installed native helper remains v5/p011 with the SHA-256 above; no driver,
USB protocol, exposure, accumulation, stop or scan-enable behavior was changed.
The `v6` prefix on the following local evidence directories identifies this
host-side comparison, not a new native runtime release.

- Completed export snapshots reuse a bounded, 16-entry metadata-service cache,
  keyed by resolved output and history paths. SQLite connections still open and
  close per operation; no connection is shared between worker threads. A reused
  history service detects and recreates a deleted optional index.
- Initial portable JSON, final portable JSON and export event fsync remain.
  Only the immediately superseded initial history-index update is omitted for
  an already-written export. Finalization still updates history. Ordinary
  experiment starts retain their previous indexing behavior. This also applies
  when reopening an existing sidecar for an updated export.
- A cache measurement pass validates each shared parent once, while inspecting
  every file's current size/type/reparse attributes. Parent validation is never
  cached across passes. Growth is still checked before each request, and a failed
  pass cannot publish partial size updates. Deletion, ownership, hash, receipt,
  quota, unsaved-evidence and durable-save guards remain unchanged.

`tmp/profile_winusb_host_v6.py` ran 60 physical 100 ms frames before and after
these changes, using the same installed v5 helper, CSV/metadata/acknowledgement
for every frame, 16 KiB log rotation and five-frame retention. No test suite ran
concurrently with either hardware profile. Frames 2–40 (39 samples) are the
unprofiled warm comparison; frames 41–60 have additional cProfile overhead.
These are new baseline measurements, not a reuse of the earlier 0.569 s result.

| Unprofiled warm median | Before | After |
| --- | ---: | ---: |
| Cache admission / maintenance | 0.12157 s | 0.03705 s |
| Complete bridge capture call | 0.59236 s | 0.49212 s |
| CSV and durable experiment metadata | 0.49601 s | 0.34282 s |
| Save acknowledgement | 0.16751 s | 0.17254 s |
| Per-frame capture + export + acknowledgement | **1.31054 s** | **1.05602 s** |

The measured complete workflow decreased **19.4%**, capture **16.9%**, and export
plus metadata **30.9%**. Save acknowledgement did not improve. Complete-workflow
medians are calculated from each frame's total, not by adding stage medians.
The corresponding 95th percentile decreased from 2.21439 s to 1.35657 s, but
these short sequential runs do not establish a latency guarantee. Initial
capture, including cache inventory and initialization, was 11.733 s before and
11.975 s after; this cold-start cost was not improved.

Both runs completed 60 frames and normal close with error 0, reaching log
segment 11 without reconnecting. Protected driver/configuration/process
snapshots matched within each run. Independent post-analysis checked all 120
raw payload hashes, CSV values, complete saved acquisition snapshots, request
and sequence correspondence, native receipt flags and final history entries.
Each run had 60 unique requests and payloads. This verifies host correspondence,
not controlled optical frame freshness or temperature. Automated scans remain
disabled, and the existing measurement processes were not restarted; an already
running application loads the updated Python code after its next normal restart.

Evidence: `tmp/v6-host-before-01/`, `tmp/v6-host-after-01/` (including
`comparison.json`), `tmp/analyze_winusb_host_v6.py`, and source snapshots in
`tmp/v6-source-before/` and `tmp/v6-source-after/`.

Validation: the focused performance/cache/metadata and two GUI modules passed
**38 tests**. Separate native fake-USB retention/rotation tests passed **2 tests**
(60 and 36 frames). The broad related invocation finished with **379 passed,
21 skipped and 2 GUI waiting failures**; it is not a full pass. Both affected GUI
modules subsequently passed alone (**17 tests**). An in-memory run of the saved
pre-change production sources also reproduced the metadata-finalization waiting
failure (369 passed, 21 skipped, 1 deselected, 1 failed). The combined-process GUI
test interaction remains unresolved; no GUI timeouts were relaxed to hide it.
Independent review caught the reopened-sidecar indexing omission; its regression
failed before the fix and passed afterward, and the final patch was re-reviewed.
