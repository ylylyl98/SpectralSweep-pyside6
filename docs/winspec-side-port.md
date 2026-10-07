# WinSpec InGaAs on the LightField side port

## Acquisition performance (bridge v12)

Bridge v12 temporarily disables `EXP_BSHOWWINDOW` during managed acquisition,
then restores the original display setting before returning a successful frame.
It uses only that parameter's getter/setter, keeps a fresh owned DocFile per
capture and leaves `EXP_NEWWINDOW` unchanged. An unsupported display getter falls
back to the original acquisition. A failed setter rejects the capture; a failed
restoration blocks further acquisition until bridge recovery. Auto-save restoration
is attempted independently. A stalled Stop prohibits all further configuration
writes. Legacy full-settings acquisition does not change the display setting.
Metadata includes `data_window_hidden` and `display_prepare` / `display_restore`
timings. `display_prepare` is part of `prepare`, so do not count it twice.

The successful five-pair v11 display probe at 500 ms / two accumulations measured
local capture medians of 1.827323 s visible versus 1.808952 s hidden (18.37 ms,
about 1%). Start medians were 0.687157 s versus 0.666098 s (21.06 ms).
Four of five capture pairs were faster with display hidden; the remaining startup
cost is substantial. All 12 frames passed final independent archive checks, and
the original display, 800 ms / one accumulation and -100 C locked settings were
restored. The report is `tmp/winspec-performance-20261001/display-result-20261001-150513-1908.json`.
Those timings exclude display setting changes, archive/report writes, cleanup and
network transfer. These figures are not MegaSweep point times.

The installed v12 bridge was subsequently verified from its server log and six
managed hardware captures (one warmup plus five measured) at 500 ms / two
accumulations. The full host-request median, including transfer/acknowledged
cleanup and display calls, was 1.846460 s (range 1.773299--1.879441 s).
Phase medians were 0.667123 s in Start, 1.075281 s waiting for the experiment,
0.045618 s after exposure and 0.000570 / 0.000393 s for display preparation /
restoration. Phase medians need not sum to the median total. The result is close
to the earlier 1.863 s v11 benchmark; separate runs do not establish a stable
17 ms speedup. All six captures passed SPE/temperature/cleanup checks and reported
display optimization active. Original 800 ms / one accumulation / one sequential
frame were restored, with -100 C locked and all running flags false.
See `tmp/winspec-performance-20261001/v12-result-20261001-152131.json` and its
`v12-raw-20261001-152131.npz` counts archive.

The staged updater is
`XP_TRANSFER\display-performance-v12\update_performance_bridge.cmd`.
When WinSpec is idle, close only the camera-server window, keep WinSpec/cooling
running, execute the updater in XP and restart the existing camera-server BAT.
The banner must show `2026-10-01-display-performance-v12`. SpectralSweep does
not need a restart for this bridge-only change.

Bridge v11 wraps the already-created `ExpSetup` COM interface with the dynamic
wrapper, avoiding the second ProgID lookup and possible COM activation per request.
The interface stays in its request's COM apartment and is never shared between
threads. Acquisition still receives a fresh empty document each time. Startup
timings now distinguish `create_document` and `start_experiment`; the existing
`start_document` is their aggregate and must not be added to them when summing
phases. These timings determine whether document creation or the WinSpec `Start`
call accounts for the measured approximately 0.69-second startup cost.
Document reuse is not enabled without evidence that it would help.

The three-pair hardware baseline from bridge v10, at 500 ms and two exposures per
point, measured 2.845 seconds for legacy Average2 versus 1.860 seconds for managed
DeviceEPF2 (medians, bridge only). SPE header validation and acknowledged cleanup
passed, and 800 ms / one accumulation was restored. A subsequent three-pair XP
hardware test verified v11 and measured 1.863 seconds for managed DeviceEPF2
(individual trials 1.863, 1.817, 1.899 seconds). No additional speed benefit was
observable versus v10's 1.860-second median. V11 phase medians were approximately
0.002 seconds for creating the empty COM DocFile and 0.697 seconds inside `Start`.
This isolates the expensive call, but does not distinguish WinSpec's internal
data-window creation from controller initialization. Both tests restored 800 ms /
one accumulation and confirmed -100 C with the temperature locked. Further
startup changes require evidence about the internal `Start` cost; offline tests
cannot establish speed improvement.

### Startup parameter-write comparison (2026-10-04)

An isolated v12 diagnostic tested whether repeated parameter writes account for
the approximately 0.67-second `Start` cost. Two independent comparisons used
500 ms / two accumulations, five alternating block pairs each, one warmup and two
measured frames per block (60 acquisitions total). Every frame used a fresh owned
document and an independent durable SPE archive. No production bridge was replaced.

| Comparison | Baseline Start median | Modified Start median | Faster block pairs |
| --- | ---: | ---: | ---: |
| Skip freshly verified unchanged auto-save writes | 627.593 ms | 626.855 ms | 3/5 |
| Keep display hidden across a block instead of per-frame toggling | 626.865 ms | 627.082 ms | 2/5 |

Auto-save was disabled in both arms of both comparisons. The first comparison
suppressed only the two repeated equal-value auto-save writes; display behavior
remained unchanged. The second kept both auto-save writes and varied only display
toggling. Successful parameter-call sequences were checked on every frame.
Start differences were inconsistent across paired blocks. Local capture medians
were 1.753078/1.734033 s for auto-save and 1.738281/1.735204 s for display, but
capture improved in only three of five pairs in each comparison. These capture
durations exclude archive/report writing, cleanup and network transfer. Neither
change establishes a reliable Start or full-point speed improvement, so neither
is enabled in production. This does not measure or establish the necessity of
either internal `setup_cont` call.

Independent checks verified all 60 SPE headers, payload hashes, counts, recipes,
temperature reports and final settings. All raw spectra were distinct and had no
full-scale summed pixels. All boundary temperature readings were -100 C. Original 800 ms / one
accumulation / one sequential frame and auto-save/display/window flags were
restored. The diagnostic used the previously reviewed error-only Stop ownership
guard and the current production temperature guard unchanged.

The completed v2 run logged an IUnknown-release exception during teardown after
restoration and a successful Quit. The diagnostic was corrected to release the
Application interface before COM teardown and to contain WMI objects in a helper
scope (Python 2 comprehension variables otherwise outlive the inventory). A
separate eight-frame v4 teardown verification passed all archive/restoration checks
without the release exception; it is not pooled with the 60-frame performance
comparison. Both runs have independent post-run process-absence checks. V1 and V3
were compiled but never launched. The final offline suite passed 126 tests, with
four pre-existing skips.

The diagnostic is `tools/winspec/startup_setparam_probe.py`; it requires a frozen
v12 diagnostic package with `confirmed_stop`, not the ordinary installed bridge.
Evidence, source hashes and independent analysis are retained under
`tmp/winspec-performance-20261001/startup-setparam-20261004/`.

### Internal Start timing profile (2026-10-04)

A 12-frame isolated run at 500 ms / two accumulations localized almost all
profiled `Start` time to its two `pl_exp_setup_cont` calls. Three blocks ran without
tracing, with tracing, then without tracing; each had one excluded warmup and
three measured frames. Bridge-reported `Start` medians were 629.278, 641.086 and
624.035 ms respectively. The first control block included one 812.601 ms outlier.
This small run supports coarse localization, not a speedup claim.

The profiler used four hardware execution breakpoints at frozen PVCAM entry and
common epilogue instructions. Every entry/exit was paired by thread, stack pointer,
saved frame pointer and return address; all returned AX=1. The two setup bodies
were entirely inside the actual COM `Start` interval on every traced frame.
Every full acquisition also contained one complete worker-thread `start_cont`.

| Profiled quantity | Median excluding warmup |
| --- | ---: |
| First setup body, minus observed debugger service pauses | 311.588 ms |
| Second setup body, minus observed debugger service pauses | 312.694 ms |
| Sum of both setup bodies, minus observed pauses | 625.968 ms |
| COM Start, minus observed pauses | 639.990 ms |
| Start remainder after both setup bodies and observed pauses | 13.146 ms |
| Both setup bodies as a fraction of adjusted Start | 97.956% |

Medians of separate quantities need not add. The raw sum of setup body spans was
626.347 ms. Observed debugger service time within Start was 1.076 ms; the separately
recorded ContinueDebugEvent-call uncertainty was 1.827 ms. Entry notification,
scheduling and hardware progress during debugger pauses cannot be reversed by
subtraction. Thus these are profiled estimates, not exact native function times.
The epilogue endpoint precedes the final pop/ret instructions. The worker's
`start_cont` body measured about 8.847 ms and crossed the COM-return boundary;
its full duration must not be added as a disjoint component of COM Start.

XP synthetic tests covered concurrent main/new-worker calls, 0/30/60/90 ms waits,
detachment during an unfinished call and successful execution after detachment.
QPC frequency matched across both processes; QPC versus GetTickCount differed by
11.257 ms over the live trace. All breakpoints were restored and the debugger
detached before settings restoration and Quit.

All 12 independent SPE archives passed header, datatype, exposure, accumulation,
ADC/gain, payload hash and count checks. All raw spectra were distinct, with
summed counts 663--1172 and no full-scale values. The production temperature guard
was unchanged; all acquisition boundary reports passed, at -100 C. Original
800 ms / one accumulation / one sequential frame, temperature setpoint and
auto-save/display/new-window flags were restored. Quit returned true, no release
exception was logged, and the independent final process check found no WinSpec or
Python owner. The final offline suite passed 146 tests, with four pre-existing
skips. No production acquisition behavior was changed.

Evidence is under `tmp/winspec-performance-20261001/startup-internal-timing-20261004/`:
`analysis-v3-final.json` contains independent verification and all per-frame
intervals; `evidence-v3/` contains raw records, console logs and archives. V1 ran
only camera-free tests; V2 was staged but never compiled or launched; V3 ran both
synthetic tests and the single hardware run. The next investigation should break
down setup's internal work and explain why both calls occur. This profile alone
does not establish that either setup call can be omitted.

### Setup and parameter programming profile (2026-10-05)

Two further isolated 12-frame runs decomposed that cost, at 500 ms / two
accumulations. Each used the same untraced/traced/untraced design, with one warmup
and three measured frames per block. No production acquisition behavior changed.

| Quantity, minus observed debugger service pauses | Median excluding warmup |
| --- | ---: |
| Run 1: both public setup bodies | 633.706 ms |
| Run 1: both controller initialization call spans | 594.907 ms |
| Run 1: initialization share of public setup | 93.878% |
| Run 2: both controller initialization bodies | 588.956 ms |
| Run 2: both nested parameter-programming bodies | 558.572 ms |
| Run 2: programming share of initialization | 94.891% |

Each percentage is the median of per-frame fractions, not the ratio of the
displayed medians. Separate medians need not add. Run 1's inner spans include the
argument setup and indirect call around the controller initializer; run 2 measures
the actual function bodies. Their endpoints and separate runs are not identical.
The initializer resolves to frozen `contrman.dll` RVA `0xDDE6`; its nested
parameter-programming target at controller slot `+0x618` resolves to RVA `0xC65FA`.
Loaded module hashes, executable section membership, target instruction bytes,
thread/frame/controller links and successful return values were verified.

Run 1 observed identical typed public setup arguments at all eight traced calls:
camera 0, one region, ROI `[0,511,1,0,0,1]`, buffer mode 2, exposure mode 0 and
exposure value 500000. This does not establish identical internal controller state.
Run 2's bounded frame-pointer traces consistently showed two distinct WinSpec
caller paths, alternating on all four traced frames:

| Path | Direct call / target RVA | Inner indirect call / return RVA |
| --- | --- | --- |
| First initialization | `0x17CA65` / `0x17CE2B` | `0x17CEA3` / `0x17CEA6` |
| Second initialization | `0x17CBCA` / `0x17D415` | `0x17D734` / `0x17D737` |

Both pass through the common WinSpec return at `0x170BB4` and the PVCAM setup
path. Exact call instruction bytes and direct targets were checked against the
hashed WinSpec binary. The analyzer checks every saved-frame link and the repeated
caller pattern. The routine names above describe their observed role; their
calling conditions have not been mapped to a supported public configuration
option. Neither dropping a call nor caching the programming routine is yet
established as safe. The next useful investigation is those caller conditions and
the expensive work inside `0xC65FA`.

Raw bridge Start medians were 629.085 / 645.927 / 624.343 ms for run 1 and
639.567 / 641.309 / 633.819 ms for run 2. Run 2's traced block ranged from 630.563
to 704.804 ms. These small samples localize cost; they do not demonstrate a speed
gain. Pause subtraction cannot undo unobserved debugger delivery, scheduling or
independent hardware progress. QPC versus GetTickCount differed by -4.006 and
-4.664 ms respectively. The function endpoints precede final pop/ret instructions.

All 24 independent SPE files passed dimensions, datatype, exposure, accumulation,
ADC/gain, payload hash and count checks; their raw spectra were all distinct.
Acquisition temperature boundary checks passed at -100 C with the existing guard.
Both runs restored the original 800 ms / one accumulation / one sequential frame,
temperature setpoint and save/display/window flags. Breakpoints were restored and
tracing detached before settings restoration; Quit returned true, console logs
were clean and independent final process checks found no WinSpec or Python owner.
Final offline validation passed 126 core tests (four pre-existing skips), 21 setup
profiler tests and 15 initialization profiler tests, run as separate suites.

The two evidence directories are
`tmp/winspec-performance-20261001/setup-internal-timing-20261005/` and
`tmp/winspec-performance-20261001/initialize-internal-timing-20261005/`.
Each retains the exact XP-tested `source-v1/`, a hash-verified `evidence-v1/` copy,
`analysis-v1-final.json`, final test outputs and an evidence manifest. The second
also contains `caller-sites-verified.json`; `caller-sites-exploratory.txt` includes
unaligned exploratory disassembly and is not the verified call-site evidence.

### State between both initializations (2026-10-05)

A further 12-frame guarded run added read-only memory snapshots at initialization
and parameter-programming entry/exit. All four traced Start calls produced eight
linked snapshots each. The native controller, its settings pointer and the WinSpec
adapter object remained identical throughout the trace. The three sampled regions
total 3,736 bytes: controller offsets `0x6600..0x6F30`, 1,280 bytes through its
settings pointer, and 104 bytes of selected WinSpec globals.
These are raw sampled ranges, not established object sizes: later constructor
inspection finds a `0x4A8`-byte settings allocation, so the last `0x58` bytes of
that 1,280-byte sample may belong to adjacent allocation memory.

Runtime slot addresses and instruction bytes confirm that the active WinSpec
adapter uses `0x1729C5` for slot `+0x5C4` and `0x1729D1` for `+0x5C8`.
Both routines simply return 1. In this adapter, the latter gate does not compare
parameters or read a dirty flag before allowing initialization. Contrman's similarly
numbered slots have different implementations; they must not be conflated.

All four pairs show the same endpoint differences:

| Native controller DWORD | First initialization exit | Second initialization entry |
| --- | ---: | ---: |
| `+0x6C20` | `0` | `0xD` |
| `+0x6E34` | `0x2000` | `0` |

Only two bytes change across the sampled controller region. The settings region
and selected WinSpec globals are byte-identical. Both parameter-programming calls
reverse those two changes; their entry snapshots are byte-identical across all
three regions. Native `+0x6E00` is zero at every captured breakpoint. The program's successful exit code
explicitly sets bit `0x2000` in `+0x6E34` and clears `+0x6C20`; its main path also
contains an explicit OR of `0x37` into `+0x6C20` before programming work.

Aligned static inspection finds an additional source of repeated preparation:
PVCAM setup first calls native slot `+0x3E0` with 1, then calls it with the stored
target count, and calls slot `+0x348(0x7FFF)` to clear status bits. The separately
retained 2026-10-03 entry trace confirms those two count requests at the exact
PVCAM return sites: 1 then 2 in eleven initializations, and 1 then 0 in the initial
warmup setup. Its modules match the current frozen binaries. This is historical
entry-only evidence, not a new hardware capture or verified setter return.
The count setter validates the request, adds `+0x6CB0` to nonzero counts, then
compares the adjusted value with `+0x67D8`. It updates that field on a difference
and ORs `0x8` into `+0x6C20`.
Thus matching endpoint values do not exclude transient configuration changes.
The exact live producer of every changed flag bit has not yet been watched.

These results establish a software path that repeatedly permits initialization
and changes preparation flags despite unchanged sampled configuration endpoints.
They do not establish that hardware requires full reprogramming each time, or that
either call can safely be removed. Uncaptured memory, pointed-to buffers, transient
writes and hardware state remain outside the snapshot comparison. No call or
parameter write was suppressed in this run, and no production acquisition change
or speed improvement is claimed.

All 12 SPE files passed independent header/payload/count checks, with distinct raw
spectra and counts 653--1186. Existing temperature checks passed at -100 C.
Original 800 ms / one accumulation / one sequential frame, setpoint and display/save
flags were restored; tracing detached, Quit returned true and an independent
process check found no WinSpec or Python owner. The final focused suite passed
150 tests with four pre-existing skips. An independent review recomputed the
archive, frame/return links, runtime mapping and all state differences.

Evidence is under `tmp/winspec-performance-20261001/initialization-state-20261005/`:
`source-v1/`, the hash-verified `evidence-v1/`, `analysis-v1-final.json`,
`static-evidence-v1-final.json`, test results and the evidence manifest.
`flag-xrefs-exploratory.txt` is search material, not the verified aligned evidence.

### Count transitions and repeated preparation verified (2026-10-05)

A separate read-only 12-frame run captured both entry and successful return of
the native count setter (`contrman+0xCAED9`) and programming function
(`contrman+0xC65FA`). Four traced Starts produced 72 paired endpoint snapshots:
28 count calls and eight programming calls. Every complete return value was 1.
The exact caller sites, stack frames, controller identity, dispatch addresses and
instruction bytes were checked against the frozen binaries.

All eight preparation sequences had the following actual transitions:

| Completed operation | Native count | Preparation flags `+0x6C20` | Status `+0x6E34` |
| --- | --- | --- | --- |
| PVCAM requests 1, return site `0x19333` | `2 → 1` | `0x5 → 0xD` | `0x2000 → 0x2000` |
| PVCAM requests 2, return site `0x19390` | `1 → 2` | `0xD → 0xD` | `0 → 0` |
| Programming returns, caller `contrman+0xDE32` | `2 → 2` | `0xD → 0` | `0 → 0x2000` |

The status change between the two count calls occurs outside the count setter.
PVCAM's intervening slot `+0x348(0x7FFF)` clears those status bits. Three additional
count calls per Start, through `contrman+0xCB91C`, requested the existing value 2
and left the count and flags unchanged. Thus same-value count writes already have
an equality check; the temporary request for 1 is what demonstrably adds mask `0x8`.
The adjustment field `+0x6CB0` was zero at all 72 captured endpoints.

Flags `0x5` already existed before every temporary count request, including the
second preparation. Aligned static inspection of the runtime-matched dispatch
targets identifies other producers: the exposure setter (`+0x190 → 0xC7A67`)
ORs mask `0x1` on its valid path, and ROI reset/reapply routines (`+0x2AC`, `+0x2B0`,
`+0x2B8`) can OR mask `0x4`. These producers were not individually traced in this run;
the live evidence proves the pre-existing `0x5`, not each instruction that made it.
Removing only the count transition therefore cannot be assumed to remove the
second programming call, especially with the always-true WinSpec gate documented
above and the programming function's own OR of `0x37`.

The two count calls are separated by mode writes and native Stop. Runtime slots
confirmed `+0xC → 0xE30D`, `+0x4DC → 0xC7141`, and descriptor override
`+0x50 → 0xD44CD`. Stop's dispatch can enter backend/transport operations; all
captured endpoints had `+0x6F94=1` and the same nonzero transport pointer. The mode
`0x121` path also contains a transport call. Endpoint snapshots do not prove these
intermediate operations are independent of the temporary count. No count write,
Stop or programming call was skipped. The useful optimization target remains the
repeated preparation path as a whole; a simple cache of the final count is not a
validated substitute for it.

All 12 archived SPE files passed independent dimension, exposure, accumulation,
ADC/gain, payload-hash and count checks. They contain distinct spectra with counts
667--1183; the existing temperature guard passed at -100 C. The original 800 ms,
one accumulation and one sequential frame, temperature setpoint, and display/save
flags were restored. Debugger restoration and detach succeeded; Quit returned true
and a fresh process check found no WinSpec or Python owner. XP Python 2.7 compile,
camera-free state/debugger selftests (including two threads and interrupted detach),
and 148 host tests passed, with four existing skips. No production acquisition
code changed and no speedup is claimed.

Evidence: `tmp/winspec-performance-20261001/count-state-validation-20261005/`,
including `source-v1/`, `evidence-v1/`, `analysis-v1-final.json`,
`static-evidence-v1-final.json`, process checks, test results and the hash manifest.
The earlier `static-evidence-v1.json` contains exploratory excerpt boundaries;
use the final artifact, rebuilt and boundary-checked by `static_validate.py`.

### Actual output sequences during repeated preparation (2026-10-05)

A read-only 9-frame run retained both programming calls and all preparation.
One traced Start produced 367 complete paired calls (two programming calls and
365 `PIPP_Output` calls), with 734 verified breakpoint endpoints. Each programming
call emitted the same **157 output tuples in the same order**. Raw Output EAX was
6 in every call; this is an API return observation, not a device acknowledgment.

Before each program, an explicit PVCAM Stop at native count 1 and an initializer's
internal Stop at count 2 emitted identical eight-output sequences. In hexadecimal
address/value notation these were `30/2, 30/0, 30/1, 40/D6, 40/D5, 30/0, 30/1, 30/3`.
The 21 outputs between programs also included two `3C/2` outputs and three exposure
outputs (`34/80, 32/FA, 34/1`). Last observed values matched the end of program1,
but 18 intervening value transitions occurred: a last-value cache does not model
pulses or hardware side effects. The later acquisition Stop was outside Start.

At all four program endpoints, PIDC `+A0` (buffer) and `+8C` were zero. Runtime
dispatch verified Disable at PIDC `2C45` and SetLongParam at `10FA`. The active
parameter `1B` branch stores `+8C`; it contains no device call. Do not equate a
PIPP_SetLongParam parameter number with a PIPP_Output address. Disable can free
the buffer and perform transport work; Enable later allocates that buffer.
These observations do not establish that programming recreates a Stop-freed buffer.

All nine SPE headers, payload hashes and count arrays were verified independently
(counts 676--1188, temperature -100 C). The original recipe, setpoint and flags
were restored, debugger detached, Quit succeeded, and no WinSpec/Python owner
remained. The focused suite passed 161 tests with four existing skips.
The first attempt remains explicitly failed: it missed attribution of three
exposure outputs before the first Stop, while its five captured frames and cleanup
were valid. Version 2 added the exact linked exposure caller chain and reran the
whole acquisition; no missing state was filled into version 1 retroactively.

The matching output sequences establish duplication, not permission to delete a
whole programming call. In particular, `C65FA` calls `D6266`, which reads address
`40`, tests bit `80` transitions, and may cause an early failure return. Its
output helpers also update the object at `[settings+48]`, outside the previously
sampled top-level settings range. The next diagnostic therefore captures inputs
and this indirect object. No programming call was suppressed and no speedup was
tested in this output run.

Evidence: `tmp/winspec-performance-20261001/communication-dependency-20261005/`,
including frozen `source-v2/`, `evidence-v2/`, `analysis-v2-final.json`, and the
separately retained `evidence-v1-failed/`.

### Input handshake and full allocation snapshots (2026-10-05)

A second 9-frame read-only run sampled the program and `PIPP_Input` entry/return.
All 32 calls completed (two programs, eight inputs inside programs, 22 inputs
outside), accounting for 64 verified breakpoint endpoints. Each program read
address `40` four times, returning full EAX values `D5D5, 5555, D5D5, D5D5`.
The first three belong to `D6266`; the fourth is the additional `C9D30` read.
Thus removing the entire first program would remove real checks as well as writes.

The four program endpoints captured exact known allocation sizes: native controller
`0x7530`, settings `0x4A8`, and the indirect object at `[settings+48]`, `0x3C`.
Each program changed only the previously identified flags/status bytes in those
three allocations; settings and the indirect object were byte-identical. However,
between programs, controller pointer fields `+7190/+7194` took each other's
previous addresses. Static inspection identifies them as original and mapped ROI
lists: each setup frees both lists and rebuilds `0x20`-byte nodes. The observed
addresses are consistent with heap reuse, not evidence of a direct pointer-swap
operation. These were outside the old sampled controller range. Endpoint equality of the
earlier smaller snapshot therefore did not establish whole-controller equality.

Actual PIPP slots were verified against executable bytes: Input `+14 → D797`,
Output `+18 → DAFB`, and parameter dispatch `+114 → DEE2`, `+11C → A270`.
The 22 outside inputs were paired and summarized by port/address/full EAX, with
endpoint clocks retained; program inputs retained their complete call stacks.
The 4096 outside-call limit is an acceptance bound, not an immediate detach trigger.
Any reader error invalidates capture; the original debugger stop conditions remain.

All nine distinct SPE payloads and headers passed validation (counts 665--1185,
temperature -100 C). Original settings and flags were restored; detach and Quit
succeeded and a process check found no WinSpec/Python owner. XP selftests included
two threads, nested calls, direct outside Input, and interrupted detach inside both
nested and outside Input. The host suite passed 136 tests with four existing skips.
Input and output traces are from separate acquisitions; their timestamps cannot
be combined into a single simultaneous bus trace. No suppression or speedup was
tested, and the production acquisition implementation is unchanged.

Evidence: `tmp/winspec-performance-20261001/input-dependency-20261005/`, including
`source-v2/`, `evidence-v2/`, `analysis-v2-final.json` and `static-evidence.json`.
Version 1 of this input probe was used only for camera-free selftests; version 2
added outside-input selftests before the sole hardware run.

### One first-program body omission tested (2026-10-05)

An isolated 10-frame experiment performed exactly one conditional omission, with
500 ms × 2 accumulations, one sequential frame and unchanged ROI. Production
acquisition remains unchanged. The experiment first captured a complete baseline
in the same owned process, then permitted `C6708 → C6D89` for program1 only when
the caller, stack, successful local value, full sampled configuration, dispatch
targets and current ROI-node contents matched. It retained the real D6266 handshake,
its failure branch, preceding status handling, the natural success tail, all Stop
calls and the complete second program. It did omit the first body's additional
C9D30 read/checks along with its programming work.

| Measured operation | Complete baseline | Conditional trial |
| --- | ---: | ---: |
| First program Output entries | 157 | 2 (handshake) |
| Second program Output entries | 157 | 157 |
| Instrumented Start | 0.652363 s | 0.361668 s |

The instrumented Start reduction was **0.290695 s (44.56%)**. Before/between/after
program output streams and the acquisition Stop stream matched the baseline
exactly. The host independently recomputed eligibility from the baseline, rather
than trusting the recorded decision. Both programs returned 1 and their native
success bookkeeping matched the original tail. Surrounding untraced Start medians
were 0.624645 s before and 0.628275 s after. These are a single instrumented
baseline/trial pair: removing 155 Output breakpoints also removes debugger overhead,
so 44.56% is not a production performance guarantee. `timing-details.json` preserves
observed pauses and resume uncertainty; subtracting pauses does not recover native
execution time.

Both ROI lists contained one independently read `0x20`-byte node. Their six content
DWORDs were `[1, 1, 512, 1, 1, 1]`, with null previous/next links. The experiment
allows rebuilt node addresses to differ only after comparing their current content;
overlapping nodes or overlap with the known controller/settings/indirect allocation
are rejected. No old node address or cached memory state is restored.

All ten SPE files passed dimensions, exposure, accumulation, ADC/gain, payload hash
and raw-count verification; every raw spectrum hash was distinct. Counts ranged
664--1170 and the trial mean was 1.000394 times the mean of the nine reference
frames. Temperature guard reports passed at -100 C. These checks do not independently
establish optical integration accuracy. Original 800 ms × 1, sequential1, setpoint
and flags were restored, the debugger detached, Quit returned true, and no
WinSpec/Python owner remained. Host tests passed 163 with four existing skips.

Native synthetic tests covered the EIP transfer, natural tail/return, two threads,
guard rejection, interrupted detach and continued calls after detach. A fault
injection exposed an inherited debugger cleanup defect: an owned TF position
mismatch was forwarded to the target. The private experiment debugger now marks
the positively identified owned TF handled before checking EIP/ESP; repeating the
injection fails the trace while the target returns normally and remains usable.
Foreign exception handling is unchanged. The earlier read-only probe debugger
files were not modified.

Experiment v1 stopped after five valid frames because a newly added report save
collided with the existing exclusive frame report filename. It never entered the
trial and never redirected execution; cleanup succeeded. That failed package is
retained. A regression test uses the real report persistence function; v2 removes
the duplicate save and recollects a fresh baseline before the successful trial.

Evidence: `tmp/winspec-performance-20261001/first-program-experiment-20261005/`,
including `source-v2/`, `evidence-v2/`, `analysis-v2-final.json`,
`timing-details.json`, the failed-v1 archive and synthetic failure evidence.
The detailed implementation plan is
`docs/superpowers/plans/2026-10-05-winspec-first-program-experiment.md`.
This result supports the fixed-recipe causal experiment; it does not enable routine
reuse or cover changed ROI/exposure, reconnect, broader error cases or long runs.

### Short repeated first-program omission validation (2026-10-05)

The follow-up deliberately used short repeated comparisons, not an endurance run.
Two clean WinSpec sessions completed **200 frames: 180 ordinary frames and 20
actual conditional omissions**, with one normal Quit/reconnect between sessions.
Each ten-frame block contained four ordinary frames, one traced full baseline,
one guarded trial, then four ordinary frames. Every block learned a fresh baseline
and restored the original 800 ms × 1 recipe and flags before proceeding. Summed
session runtime was 441.978 s (about 7.4 minutes; excludes time between sessions
spent inspecting evidence). A third package was compiled and self-tested but was
not launched for acquisition after the requested short-test coverage was reached.

| Recipe | Ordinary reference frames | Actual optimized frames |
| --- | ---: | ---: |
| 500 ms × 2 | 153 | 17 |
| 250 ms × 2 | 9 | 1 |
| 800 ms × 1 | 9 | 1 |
| 1000 ms × 1 | 9 | 1 |

Across the 20 instrumented comparisons, baseline Start median was **0.660718 s**
and optimized Start median **0.384033 s**. The paired saving median was **0.275890 s**
and paired percentage reduction median **42.07%**. Each trial omitted 155 first-body
Output entries; its real handshake, full second program and other output streams
matched its own baseline. As before, fewer debugger breakpoints also reduce probe
overhead; this is not a native production timing measurement.

All 200 archived SPE payload hashes were distinct and matched their reports.
Geometry, exposure, accumulation count, datatype, ADC/gain and temperature reports
passed; all reported temperatures were -100 C. Trial mean differences from each
block's nine references ranged from -0.4629% to +0.4713%, averaging +0.00328% across
the 20 blocks. Trial pixel RMSEs were comparable to reference leave-one-out RMSEs;
the largest trial/reference-maximum ratio was 1.0231. These are descriptive
comparisons under uncharacterized illumination, not an optical equivalence test.

The host analyzer initially rejected the single-accumulation files because its
old fixed-recipe check required 6148 bytes and signed 32-bit pixels. All ordinary
and optimized frames in both single-accumulation blocks actually used SPE type 3,
unsigned 16-bit pixels and 5124 bytes; double accumulation used type 1 and 6148
bytes. The acquisition validator already handled both correctly. The host decoder
now accepts these two types, requires exact payload length, checks metadata and
type consistency within each block, and rejects malformed files. Six regression
tests cover both types, truncation, extra bytes, wrong dimensions and unsupported
types. Frozen XP runtime and omission guards were not changed to pass this check.

Both sessions restored settings and flags, detached their debuggers, confirmed
Quit and process exit. No WinSpec/Python owner remained. Before reusing the
single-run diagnostic, the private runner gained a pre-entry check that preserves
an existing recovery owner instead of clearing its recovery marker. Independent
review and host checks passed (**157 tests, four skips**). Each closed session has
451 copied, hash-verified evidence files; analysis of source and copied evidence
agrees.

Production acquisition remains unchanged. This test covers repeated guarded
omissions after ordinary warmup/baseline frames, selected recipe transitions and
one clean application reconnect. It does **not** cover uninterrupted all-optimized
acquisition, optimization on the first frame after a parameter change, ROI changes,
physical disconnect/error recovery, or long-duration operation. An endurance test
is optional evidence for duration-specific claims, not a prerequisite imposed by
this short validation.

Evidence: `tmp/winspec-performance-20261001/repeated-start-validation-20261005/`,
including `aggregate-v1.json`, both `evidence-v1-s*/` directories, copied-file hash
manifests, source/copy analyses and `tests-final.txt`. Plan:
`docs/superpowers/plans/2026-10-05-winspec-short-repeat-validation.md`.

### Thirty consecutive optimized captures verified (2026-10-05)

A follow-up completed **30 adjacent, actually optimized acquisitions** at fixed
500 ms × 2, sequential1 and unchanged ROI. The complete sequence was four ordinary
frames, one traced full baseline, 30 guarded trials, then four ordinary frames:
39 frames total. No ordinary acquisition, recipe reapplication or replacement
baseline occurred inside the 30-frame trial segment. All trials matched the same
original baseline and the same WinSpec PID, native controller and thread. Every
trial made exactly one first-body EIP redirect; there were zero fallbacks. Each
retained the real handshake, complete second program and all other output streams.

| Observation | Result |
| --- | ---: |
| Consecutive actual omissions | 30 / 30 |
| Ordinary reference frames | 9 |
| Optimized Start median | 0.380696 s |
| Optimized Start range | 0.333989–0.399451 s |
| Single traced full-baseline Start | 0.827916 s |
| Surrounding untraced ordinary Start median | 0.628655 s |
| Interval between trial acquisition calls | 0.571908–0.712898 s |
| Diagnostic runtime | 98.033 s |

The debugger was detached and reattached between frames so the existing single-
Start trace and state gate could remain unchanged. Those gaps issue no camera
commands but make this a sequence of consecutive optimized captures, not zero-gap
camera streaming. The one instrumented baseline is not 30 independent paired
comparisons; its longer Start and unequal probe overhead should not be treated as
a reliable new production speedup percentage.

All 39 SPE files had distinct raw payload hashes and passed geometry, datatype,
exposure, accumulation, ADC/gain and raw-count checks. All temperature reports
passed at -100 C. Trial mean differences from the nine reference frames ranged
from -0.5266% to +0.6584%, averaging -0.01999%. Trial pixel RMSEs were 14.04–16.90
counts versus 14.44–16.35 for ordinary reference leave-one-out comparisons. These
are comparable observed fluctuations, not proof of optical equivalence under
independently controlled illumination.

Full trace/config files are retained separately; cumulative reports bind their
hashes to each frame index and Start time window. Host and independent review
verified 31 traces, 62 program calls, 6851 owned breakpoint/step completions and
372 current ROI-node snapshots. QPC frequency, nonnegative trace duration and
containment of each Start within its trace were checked. The initial baseline's
two complete 157-output streams were explicitly compared. Any fallback or cleanup
uncertainty stops later acquisitions; tests cover both paths.

Original settings/flags were restored, every debugger detached, Quit and process
exit were confirmed, and no acquisition owner remained. **153 host tests passed,
four skipped**; XP compile and camera-free selftests passed. The 346 copied evidence
files match their shared originals, and both copies reproduce identical analysis.
Production acquisition remains unchanged. This closes the previously untested
case of consecutive optimized frames for this fixed recipe; it does not establish
zero-gap operation, first-frame-after-configuration behavior or long-term operation.

Evidence: `tmp/winspec-performance-20261001/consecutive-start-validation-20261005/`,
including `source-v1/`, `evidence-v1/`, `analysis-v1-final.json`,
`analysis-v1-copy-final.json`, copied-file hashes and final verification records.
Plan: `docs/superpowers/plans/2026-10-05-winspec-consecutive-optimization-validation.md`.

### Resident lightweight Start validation (2026-10-05)

A bounded follow-up kept one debugger attached for **20 adjacent optimized
captures**, removing the per-output PIPP breakpoint while preserving the three
program-entry/body/exit breakpoints and all six native state snapshots per Start.
The same full baseline and state gate were reused throughout. An explicit,
monotonically numbered open/close protocol re-arms the gate only after successful
completion of the preceding frame. Each close checks two successful programs,
six endpoint hits and owned step completions, one first-body redirect, the natural
tail state, the second program's configuration, and the enclosing Start times.
Complete frame evidence is flushed before its close acknowledgement. Any protocol,
read, caller, byte, exception or persistence error disables further omission and
requests restoration through the debugger event loop. An uncertain detach retains
the acquisition owner. Disabled, unused debug-register slots are preserved.

The one-session sequence used 500 ms × 2, sequential1 and unchanged ROI: four
ordinary frames, one full traced baseline, three optimized frames with the previous
per-frame debugger, twenty resident optimized frames, another three per-frame
controls, and four ordinary frames. All **35 frames** completed; all **26 requested
omissions** occurred, including every resident frame, with zero fallbacks and no
recipe writes inside the sequence. Total run time was **79.781 s**.

| Measurement | Per-frame full probe | Resident lightweight probe |
| --- | ---: | ---: |
| Optimized Start median | 0.379342 s (6 frames) | 0.374929 s (20 frames) |
| Start-to-Start interval median | 2.078783 s (4 within-block intervals) | 1.610201 s (19 intervals) |
| Acquire-return to next acquire-call gap median | 0.596587 s | 0.120197 s |
| Full per-frame work cycle median | 2.076088 s | 1.609259 s (18 interior frames) |

The steady Start-to-Start interval decreased **22.54%**, with about **476 ms less
inter-frame handling**. Start itself changed by only about 4 ms; this result supports
removing diagnostic lifecycle overhead, not another large hardware-programming
speedup. The resident block took 33.631 s including its first attachment and final
detach/evidence processing, or **1.681565 s/frame**. Compared with the controls'
2.077164 s mean full cycle, this amortized saving was **19.05%**. The resident first
and last cycles were 1.869 s and 2.730 s respectively; these costs are included in
the block mean. The control interval sample is small, and both paths remain
instrumented experimental paths rather than production-native benchmarks.

All SPE files had distinct payloads and passed 512×1 signed-32-bit geometry,
500 ms exposure, two accumulations, ADC/gain, count and archive checks. Every
temperature report passed at -100 C. Resident mean-count differences from the
15 surrounding reference/control frames ranged from -0.5869% to +0.6963%; pixel
RMSE was 14.45–16.36 counts. Illumination was not independently characterized, so
these measurements do not establish optical equivalence. The six fully traced
controls still demonstrated first-program outputs reduced from 157 to 2 and the
second full 157-output stream preserved. **Resident frames do not directly observe
PIPP output streams**; they verify the guarded redirect and complete second-call
endpoint/state evidence.

Host analysis verifies frozen source/XP compilation hashes and selftests, all five
binary identities, loaded endpoint bytes, object/ROI pointers and function slots,
Start/QPC bounds, per-frame/global record agreement, and raw SPE data. Shared and
copied evidence reproduce identical results. **165 host tests passed, four skipped**;
XP camera-free tests cover native redirects, nested calls, error recovery, resident
command/ack failures, disabled Dr3 restoration, and a stop requested before open.
Two additional host regressions verify close-evidence and close-ack failure after
the boundary counter advances. Earlier v1/v2 selftest harness failures are retained;
neither version launched a camera acquisition.

Original 800 ms × 1 settings, sequential1, flags and cooling setpoint were restored.
The debugger detached, WinSpec Quit returned true, process exit was confirmed, and
no WinSpec/Python acquisition owner remained. Production acquisition was unchanged
by that standalone experiment; the optional integration below was validated later.
The resident experiment remains explicitly bounded to at most 30 frames and a
90-second debugger deadline; it is not a general long-running backend. This result
requires neither a multi-hour run nor a claim about long-duration reliability.

Evidence: `tmp/winspec-performance-20261001/resident-start-validation-20261005/`,
including `source-v3/`, the 329-file `evidence-v3/`, `analysis-v3.json`,
`analysis-v3-copy.json`, `cycle-comparison.json`, copied-file hashes,
`tests-final.txt` and the final independent review.

### Optional production Start acceleration (2026-10-05, v13)

Build `2026-10-05-optional-start-acceleration-v13` exposes the verified optimization
through normal guarded TCP capture. `LF6Config.winspec_start_acceleration` defaults
to **false**. Under **Advanced connection settings**, select the WinSpec acquisition
backend and enable **Enable WinSpec Start acceleration (experimental)** before
connecting InGaAs. Disconnect before changing this choice. PVCAM cannot use it.
Restart SpectralSweep and the XP bridge to load the updated files; an older bridge
rejects explicit opt-in before configuration writes. Deploy the bridge, temperature
guard, acquisition owner, Stop guard, and the complete `start_acceleration` package;
the updated `update_temperature_bridge.cmd` includes these dependencies.

Each connection has a new session ID. The first frame uses a complete traced Start.
Matching subsequent frames may omit the first program body; the second program
remains complete. Native state/caller/byte/return/step checks remain mandatory.
The initial eligibility envelope is the five frozen binary hashes, full 512×1,
Free Run, one sequential frame, and configured exposure × accumulations ≤1 s.
Unsupported cases capture normally. Changing settings, reconnecting, stopping, or
returning to ordinary capture invalidates the baseline. Exposure keys use the SPE
float32 representation and exclude observed readout duration; native state is
still compared without this rounding.

A parameter change can need a settling frame: in the real 500 ms ×2 →800 ms ×1
transition, the two complete programs differed at controller `+0x67D8` (2→1), and
their 157-item output streams differed at item 24 (`0x3C`, 2→1). This frame was
delivered through ordinary full Start as **fallback**. The next frame established
a full baseline and the following frame optimized successfully. At most two
successive ineligible full baseline frames are attempted; continued ineligibility
uses ordinary capture until settings/session invalidation. No frame is acquired
twice, and no native gate is relaxed.

Resident sessions rotate after 30 optimized frames or 60 s at admission. A 90 s
idle deadline cannot detach an already opened frame. Only verified detach allows
replacement. Up to four successfully retired diagnostic groups are kept per
bridge process; failed/cancelled groups and pre-existing groups are preserved.
Thus repeated restarts or failed runs can retain additional diagnostic files.
Read/trace/thermal/ownership failures stop continuation. Stop remains reachable
while Start blocks; after Start returns, cancellation reconfirms Stop before
cleanup. Once native acquisition is confirmed complete, later Stop cancels
delivery without issuing another native Stop during configuration restoration.

The request apartment retains factory/by-reference documents before timing and
through data receipt. Complete sent frames require WXAK and confirmed Save/Close
before temporary-file removal; the client requires the final cleanup receipt.
Confirmed cancellations before delivery preserve a recovery SPE without waiting
for a nonexistent receipt. Unknown Stop/detach/cleanup retains its owning apartment
and rejects subsequent acquisition/configuration. No cooling settings are changed.

The accepted v3 run took **50.829 s**, with **26 delivered frames** (8 ordinary,
4 baselines, 13 optimized, 1 settling fallback) and one additional native Stop
attempt whose frame was rejected and saved locally. All 10 adjacent continuous
frames optimized. Stop/restart, exposure/accumulation change and fresh-session
reconnect passed. All 26 SPE payloads were distinct and matched the host bytes,
512×1 geometry, exposure/accumulation metadata, raw counts and mean normalization.
Every temperature report passed at −100 °C. Original 800 ms ×1, sequential1,
display/autosave flags and cooling setpoint were restored; all debug sessions
detached, owned documents closed, WinSpec Quit succeeded and process exit was
confirmed.

| Measurement | Ordinary capture | Stable optimized capture |
| --- | ---: | ---: |
| Start median | 0.617311 s | 0.368961 s |
| Acquire request to processed UI spectrum median | 1.750998 s | 1.573450 s |

Start decreased **40.23%** and the measured steady UI path **10.14%**. The samples
are seven ordinary frames (excluding the first cold frame) and nine stable
optimized frames (excluding the first resident attachment). The 11 opt-in capture
requests including baseline and first attachment averaged **1.704876 s**; this
request-time average excludes gaps and final session release, so it is not a
complete cycle benchmark. Baseline creation makes short sequences less beneficial.

The detector, TCP receipt protocol, production adapter, real QThread/signal path
and offscreen SpectrumPanel processing were exercised. LightField optics were an
explicit fixture; actual optics latency, connection/Apply button interaction and
physical screen paint were not measured. Optimized light traces verify six
endpoints and the guarded redirect, **not direct PIPP output-stream observation**.
These checks establish data-path integrity, not optical equivalence or unattended
long-duration reliability. No hours-long endurance run was required.

Host core checks: **194 passed, 4 skipped**; UI integration: **19 passed**. Frozen XP
Python 2.7 compilation and native camera-free gate/redirect/nested/resident tests
passed. Evidence and independent analysis are under
`tmp/winspec-performance-20261001/formal-start-integration-20261005/`:
`source-v3/`, `evidence-v3/` (277 hash-verified copied files), `host-v3/` and
`analysis-v3.json`. v1 selftests and the v2 conservative-fallback acceptance failure
are preserved; v2 also restored settings and exited safely.

The accepted 19 bridge/package source files were installed in `C:\WinSpecRemote`
and matched the frozen hashes; 18 Python files were freshly compiled on XP after
backing up prior source/bytecode. Backup:
`C:\WinSpecRemote\before-start-acceleration-20261005-110604`.
The service was left stopped and no WinSpec/Python owner remained after validation
and installation. The currently open desktop app must be restarted to load its
new opt-in control. Installation/acceptance hashes are retained alongside the
evidence in `installation-verified.json` and `evidence-v3/installation.json`.

### Remaining second-program cost: offline attribution (2026-10-05)

The next hotspot is a specific serial configuration block, rather than an
identified single long delay. Re-analysis of the 13 delivered production light
traces gives a second-program median of **274.498 ms**: prefix **10.707 ms** and
body/tail **264.157 ms** (separate medians do not necessarily add). This mixes
11 frames at 500 ms ×2 with two at 800 ms ×1; their separate second-program
medians are **274.498 ms** and **275.054 ms**, respectively. These light
traces do not contain per-output timing. They are distinct from the earlier
complete paired-output capture below.

The saved `communication-dependency-20261005/evidence-v2` capture was independently
revalidated. In its second program, the 157 observed `PIPP_Output` calls have a
median duration of **1.321 ms**, maximum **2.951 ms**, and summed intervals of
**231.905 ms**. Of those calls, **128** belong to `contrman+C6DC7`, called from
`C6CD1` and returning at `C6CD6`; all 128 target `0x4A`.

| Second program, earlier paired trace | Observed wall interval | Included observed debugger pause | Resume-call uncertainty |
| --- | ---: | ---: | ---: |
| Whole program | 308.730 ms | 66.529 ms | 43.117 ms |
| 128 serial output-call intervals, summed | 182.922 ms | 33.553 ms | 25.669 ms |
| First serial-output entry to last return, including gaps | 225.096 ms | 52.497 ms | 31.396 ms |

The last row is a span across observed outputs, not the complete function's
entry/exit interval. Gaps include untraced header batch calls, local work and
debugger effects. Subtracting observed pauses does not recover native execution
time; neither this 225 ms span nor its proportion can be applied to the different
274.498 ms production measurements as predicted savings. Software output returns
also do not establish USB or device acknowledgment times.

The serial block contains two groups of **52 + 76** single outputs. The first
has two leading edges, 16 data bits at three outputs per bit, and two trailing
edges. The second has two leading edges, 12 data bits plus 12 zero bits, again
three outputs per bit, and two trailing edges. The observed bit streams are
`0000000010001000` and 24 zeros. A low/high/low clock triplet and repeated low
values carry protocol meaning; last-value deduplication would corrupt the stream.

Frozen disassembly identifies an existing batch path: `contrman+39400` constructs
24 byte values for an eight-bit command header and invokes import ordinal 6,
`PIPP_Output_Multiple`. Its wrapper dispatches through port slot `+48`. The
PIPP constructor assigning the known single-output slot `+18 -> DAFB` also assigns
`+48 -> DC92`. DC92 constructs a three-byte address prefix followed by one
`02,value,00` triple per byte-valued output, then calls
`USBDRVD_PipeWriteTimeout`. These header calls are outside the saved single-output
trace's coverage; actual runtime slot `+48` and bulk completions were not captured.

This supports a concrete **candidate**, not an implemented speedup: retain the
two existing header batches and eight single edge writes, and replace only the
**120 data outputs with five batches of 24 values**. Each modeled packet is 75
bytes, the same size as the existing header packet. Offline decoding confirms
every data value and clock edge remains in its original order across chunks for
both observed programs. It does not establish device timing equivalence.

The batch API is not a drop-in replacement. DAFB passes timeout `10000` and returns
the reported transferred count; DC92 passes `-1`, ignores the lower write result
and returns constant zero. A live candidate must first verify the actual batch
slot and the lower transport's completion/byte-count contract, and provide bounded
failure handling while preserving owner/Stop behavior. A short controlled A/B
would then check pulse ordering, acquisition metadata/data, Stop/restart and
actual timing. No caching of hardware state or deletion of the second program is
justified by this analysis.

Evidence: `tmp/winspec-performance-20261001/second-program-analysis-20261005/`
contains `analyze.py`, `test_analysis.py`, `analysis.json` and frozen-byte
`static-evidence.json`. The analysis rechecks the prior 215-file communication
manifest and 805-file production manifest. Five focused offline checks exercise
the archived pulse streams, packet reconstruction, lost edges, deduplication,
malformed trailers and non-byte values. This investigation performs no acquisition
and makes no production or installed-bridge changes.

### Second-program batch experiment: short hardware acceptance (2026-10-05)

The temporary `batch-output-validation-20261005/source-v8` experiment passed
on the installed XP hardware. The existing first-program skip was **disabled
in both arms**, and the first program still ran in full. This isolates a second
optimization; these results do not measure the two optimizations together.

Four alternating ordinary/batch pairs at 500 ms x2, after one warmup, gave:

| Metric (median of four per arm) | Ordinary | Batch | Reduction |
| --- | ---: | ---: | ---: |
| Complete bridge Start, including diagnostic arm/disarm | 0.623587 s | 0.442046 s | 0.181541 s (29.1%) |
| Second native program | 272.545 ms | 88.086 ms | 184.459 ms |
| First native program | 270.539 ms | 269.660 ms | 0.879 ms |
| Acquisition request to processed panel | 1.744236 s | 1.556179 s | 0.188057 s (10.8%) |

The panel ran offscreen through the production adapter/QThread/processing path;
LightField optics were a fixture. Do not add this measured 0.182 s saving to the
earlier first-program result and present the sum as a measured combined speedup.

The helper batches only the 120 data pulses in program two into five 75-byte
writes. All 157 logical single-output requests retain their original order and
values; 37 remain forwarded single calls. The eight serial edge writes and two
24-value header calls remain original. Runtime tracing also revealed two earlier
`PIPP_Output_Multiple` calls at caller `6E88`, command `20`, with lengths 520 and
512. They remain original and are checked by call shape; their payloads were not
recorded or compared. There are therefore four original Multiple calls per
program, not just the two serial headers.

Runtime module hashes, relocated code, persistent command pipe and overlapped
handle identity are checked before installing three temporary hooks. New packets
use a retained OVERLAPPED/event/buffer and require a confirmed full 75-byte write.
A timeout cancels on its issuing thread and checks completion; any failed or
uncertain write parks further output and retains the owner. The wait limits do
not guarantee that the device's WriteFile call itself cannot block, and failure
parking is not automatic recovery. Hardware timeout faults were not injected.

Acceptance delivered **16 unique frames**, including **7 batch frames**, plus
one native Stop that correctly rejected its frame and retained a separate SPE.
The Stop acquisition was also batched, giving **17 Starts, 34 native programs,
40 new packets**, and 6,462 committed trace rows. Every packet expanded to exactly
the baseline pulse sequence. All 16 saved SPE payloads matched their network
payloads byte for byte; accumulated counts, normalization and panel values also
matched. The 800 ms x1 pair correctly used the SDK's 16-bit SPE type, while x2
used 32-bit accumulated counts. Stop/restart, changing to 800 ms x1 and returning
to 500 ms x2 passed. Fresh boundary temperature checks stayed at -100 C and locked.
These checks establish digital data integrity in this short run, not equality of
independent noisy exposures, optical equivalence, or indefinite reliability.

The earlier v4 attempt stopped during its second ordinary frame, before any new
batch write. Its guard assumed only two Multiple calls and rejected disarm
without recording the precise reason. All recorded native calls had later
returned, and no helper-owned write was pending; the later quiescent state is
consistent with a transient busy window, but v4 did not capture its cause.
Checked teardown restored the three original pointers,
recipe and flags, then confirmed normal WinSpec exit before retiring the inert
bridge. The revised helper distinguishes the two non-header calls and returns
the actual disarm rejection mask. v8 recorded 16 temporary `active`-only
rejections outside a program, followed by 17 successful disarms. It waits only
after a confirmed busy rejection; unknown completion and partial writes cannot
be retried. XP regression tests also cover faults in diagnostic logging without
losing the owner-retention latch or the original error.

The accepted run restored 800 ms x1, original display/autosave flags and all
three hooks, confirmed normal WinSpec exit, and left no WinSpec/Python owner.
No production bridge, host implementation or installed package was changed.
At that checkpoint, first-body acceleration was the only deployed optimization.
The combined implementation and its separate acceptance are described below.

Evidence: `tmp/winspec-performance-20261001/batch-output-validation-20261005/`,
especially `source-v8`, `evidence-v8`, `host-v8`, `analyze.py`, `analysis.json`,
`evidence-v4-failed-recovered`, and `final-processes.json`. The exact DLL was tested
on XP with seven I/O fault scenarios, seven native replay cases and nine Python
regressions before acquisition. Frozen sources, binaries and compile commands are
hash-linked; independent review covered both the implementation and results.

### Combined production Start acceleration (2026-10-05, v14)

Build `2026-10-05-combined-start-acceleration-v14` combines the verified first-body
skip with five 75-byte writes built from the current second program. The existing
Start acceleration checkbox remains opt-in and defaults off. The original
program callback and its stack are preserved; only two output imports are hooked.
The first skip must finish its natural tail and verified instruction step before
the current second-body state can grant generation-specific batch permission.
The two prefix outputs and both original 520/512-byte Multiple calls are kept.

The accepted 27-file package was installed and recompiled on XP after backing up
the previous files to `C:\WinSpecRemote\before-start-acceleration-20261005-160341`.
Installed hashes match both the frozen acceptance source and the workspace.
Production regression: 174 passed, four skipped. The updated manual installer
includes all new Python modules, the native DLL and its audit. Native C sources
and frozen build/replay recipes are retained in
`tools/winspec/start_acceleration/native_source/`. The bridge remains stopped
after deployment; start it through the usual launcher before using the opt-in
checkbox. The deployment receipt is `installation.json` in the evidence root.

The final frozen formal acceptance (`combined-start-formal-20261005/source-v3`)
delivered 18 spectra and rejected one actively stopped acquisition. Independent
review matched all SPE payloads to wire data, rebuilt 40 packets (3,000 bytes)
from each frame's current pulses, and checked 96 state/ROI snapshots plus the
original debugger/step evidence. Stop required a fresh baseline on restart;
500 ms x2 -> 800 ms x1 -> 500 ms x2 invalidated and rebuilt the baseline correctly.
Default-off and disabling after combined acquisition both used ordinary Start.

For matched 500 ms x2 settings, excluding warmup and baseline acquisitions:

| Path | Frames | Median Start |
|---|---:|---:|
| Ordinary, before/after | 2 | 0.617799 s |
| Combined | 6 | 0.215975 s |

This short final check saved about 0.402 s (65%) inside Start. The separate v4
three-way comparison measured ordinary/first-only/combined at
0.618610/0.384683/0.215711 s: the batch stage added about 0.169 s of savings beyond
first-only. These are measured combined results, not sums of separate estimates.
Offscreen UI delivery also includes debugger preparation, transfer and display;
its timing is a different interval. Optics were a test fixture, so this acceptance
establishes digital integrity, not optical equivalence between noisy exposures.

The formal lifecycle keeps one DLL, one native event and two remote allocations
(DLL path and reusable CONTROL buffer) per owned WinSpec process. Successful
retirement verifies debugger detach, durably archives native records and hashes,
restores imports, then acknowledges the exact generation/frame/row descriptor
before clearing the bounded record buffer. Eight real retirement groups reused
the same DLL/control addresses and ended with zero records and restored imports.
Camera-free checks additionally covered 600 real CONTROL calls, eight reinstalls
(47 -> 47 handles), 121 frames crossing the record capacity through resets, and
2,000 native event-reuse cycles. Unknown completion retains resources and blocks
further acquisition; it is never retried. The normal session limit remains 30
frames/60 seconds; the acceptance used a limit of two to exercise retirement.

Formal v1 stopped before any Start because the test harness did not handle Python
2's `None` module placeholders. Checked normal Quit preceded retirement of that
idle harness; its WMI cleanup-reporting TypeError is retained. Formal v2 completed
its native/data scenarios but its host check failed when test-only evidence
copying held the status lock. Formal v3 adds a bounded retry solely for three
explicitly busy GET requests in the test client (one retry occurred). Production
client behavior is unchanged; Start, setting, Stop and release requests are never
retried. All failed attempts are preserved alongside the accepted evidence.

Evidence lives under
`tmp/winspec-performance-20261001/combined-start-formal-20261005/`:
`source-v3`, `evidence-v3`, `host-v3`, `independent-review-formal.json`, and
`final-processes-v3.json`. Settings/flags were restored, WinSpec quit normally,
and the final process check found no WinSpec/Python owner. The Stop occurred
after native programming completed; uncertain in-flight writes were covered by
camera-free fault tests. No hours-long endurance run or indefinite reliability
claim is made.

### Offline document reuse probe

Two five-pair XP runs on 2026-10-01 did not establish a reliable speed benefit.
Local capture medians were 1.783 s fresh versus 1.811 s reused in the first run,
and 1.860 s fresh versus 1.800 s reused in the second. `Start` medians were
0.661/0.669 s and 0.699/0.668 s respectively. These timings are provisional:
both runs failed the final archive check because the warmup SPE's contents had
changed. The exact operation that rewrote that file has not been isolated.
The returned COM identity matched for all reuse trials, and independent counts
snapshots were retained, but neither run is an accepted acquisition-integrity
result. Document reuse remains disabled in normal acquisition.

Both runs restored 800 ms, one accumulation and one sequential frame. A subsequent
read-only bridge query confirmed it online and idle, at -100 C and locked.
Raw reports and the comparison are retained in
`tmp/winspec-performance-20261001/`; see `startup-reuse-analysis.json`.
The launcher below is an experimental diagnostic, not a recommended optimization.

The staged `XP_TRANSFER\startup-reuse-probe` folder contains
`run_startup_reuse_probe.cmd` and `startup_reuse_probe.py`. Close only the XP camera
server, leave WinSpec/cooling running, and run the CMD in XP. The probe imports
the installed v11 bridge; it does not replace it or change normal acquisition.
It exclusively reserves port 5000 before COM initialization, preventing concurrent
bridge/probe startup. It has one COM apartment for the full test and accesses no
LightField or SMU objects. Measurements using other detectors may continue.

At 500 ms and two accumulations, one dedicated warmup document is followed by five
interleaved fresh-document/reused-document pairs. Each acquisition uses the real
temperature watchdog and SPE validation. Reuse requires the returned document's
COM IUnknown identity to match the supplied bridge-owned document. Every verified
frame, its counts and metadata receive an exclusive-created, flushed/fsynced local
report snapshot before the document can be reused. All uniquely named SPE files
remain in `C:\WinSpecRemote\startup-probe-<timestamp>-<pid>`, and their headers and
raw checksums are rechecked at the end. It never reads or closes user documents.

The original exposure, accumulations and sequential frame count are restored on
completion or failure; automatic saving uses the bridge's existing per-frame
restoration. A stalled watchdog Stop prohibits further configuration writes and
reports `restore_ok=false` for explicit recovery. Failures halt the test and
retain affected documents/SPEs. Restore errors fail the overall probe. The final
report is also copied to the shared probe folder as `result-<timestamp>-<pid>.json`.
Restart the normal camera server after a successful probe. Start-time medians are
the primary A/B result; local capture durations exclude network transfer and the
diagnostic report writing, so they are not equivalent to MegaSweep point duration.

### Startup controller inspection

A read-only inspection of the successful v11 benchmark gives median `Start`
times of 0.727 s for six single-accumulation captures and 0.697 s for three
double-accumulation captures. `WaitForExperiment` medians are 0.572 s and 1.072 s;
the extra 500 ms appears in the wait rather than startup. This supports a fixed
per-start cost, but does not identify which internal WinSpec operation causes it.
`EXP_READOUT_TIME` reports 0.5171 s even after restoring 800 ms exposure, so it
must not be interpreted as 0.5171 s of independently measured USB transfer time.
The latest Roper-device enumeration in the available VMware log (2026-09-30)
reports `speed:high`, connected to EHCI port 0. There is no evidence here of a
USB 1.1 fallback; these records do not measure per-call USB latency.

The [OMA V manufacturer manual](https://www.pi-j.jp/support/manual/OMA-V_InGaAsSystemManual.pdf)
(pp. 59, 62) states that only Fast Mode is valid for OMA V InGaAs. Generic CCD
Safe/Fast advice therefore does not establish an optimization for this detector.
Its USB interface also does not support the manufacturer's legacy EZ-DLLs API
(pp. 30, 113); a direct DLL bypass is not an established alternative here.

`XP_TRANSFER\startup-readonly-audit\run_startup_readonly_audit.cmd` runs a
one-time getter-only diagnostic using the installed bridge's COM bindings.
Close only the XP camera server first; keep WinSpec/cooling running. The script
reserves port 5000, refuses a busy/unknown camera state and reads identity,
driver/interface, delay, cleans, shutter compensation, display and correction
flags. Missing enums/failed getters are recorded as unsupported, without numeric
guesses. Direct `GetParam` is used without ROI-object discovery or ValidRange
fallbacks. No `Start`, `Stop`, `SetParam`, DocFile or spectrometer calls are made.
It compares available exposure, ROI enable, geometry and other scalar settings
before and after; the report names that comparison scope. It does not restore an external
change. The diagnostic does not change production acquisition or replace the
installed server. Restart the existing camera-server BAT afterwards.
The local and shared `startup-audit-<timestamp>-<pid>.json` reports are needed
before attributing startup overhead to any of these options.

The XP audit `startup-audit-20261001-144806-1732.json` completed with all 13
available configuration fields unchanged. It reports `EXP_DELAY_TIME=0`, one
clean and one strip per clean, and background/flatfield/cosmic corrections all
disabled. Ordinary `GetParam` calls have a median of 0.160 ms and a combined
duration of 4.03 ms for the 27 diagnostic parameters; ExpSetup creation took
15.7 ms. These reads do not show the acquisition's 0.7-second startup delay,
but do not establish how much controller/USB work happens inside `Start`.
The display-related flags are `EXP_BSHOWWINDOW=-1` and `EXP_NEWWINDOW=1`.
These flags motivated the subsequent controlled display test described above.
Retain a fresh explicitly owned document per capture; do not enable document
reuse or change cleans based on this audit. Shutter compensation, driver version
and 9 other diagnostic parameters were unsupported, so their values are unknown.
The original 800 ms / one accumulation and -100 C locked readings were retained.

### Offline display optimization comparison

`XP_TRANSFER\startup-display-probe\run_startup_display_probe.cmd` compares the
original visible setting with `EXP_BSHOWWINDOW=0`, keeping `EXP_NEWWINDOW`
unchanged and supplying a fresh explicit DocFile for every capture. At 500 ms /
two accumulations, both display modes receive a warmup, followed by five pairs
with alternating order. SetParam status and display readbacks must agree before
the next capture. Cleans, shutter, geometry, gain and other acquisition options
are not varied. No LightField or SMU objects are accessed.

Before the bridge's normal Save/Close/delete cleanup, every frame receives a
unique independent SPE copy whose path is never passed to WinSpec. That copy is
created exclusively in binary mode, flushed/fsynced, and validated against the
acquired header and raw bytes; an independent counts/metadata snapshot is also
persisted. All archived SPE checksums are checked again after the final capture.
Acquisition failures preserve unacknowledged documents and files. Exposure and
display restoration are attempted independently; a stalled Stop skips all writes.
Any restoration failure invalidates the result. Reports and SPE archives remain
in `C:\WinSpecRemote\display-probe-<timestamp>-<pid>`, with the report also written
to the shared folder as `display-result-<timestamp>-<pid>.json`.

Close only the XP camera server before launching the probe; leave WinSpec/cooling
running, then restart the existing bridge after a successful test. The staged
folder includes `startup_reuse_probe.py` solely for its pure validation/report
helpers; its reuse experiment is not invoked. The installed production bridge is
not replaced. Capture timings exclude display setting changes, independent
archive/report writes and cleanup, and are not full MegaSweep point times.
This diagnostic requires the original v11 bridge and must not run against v12,
whose managed acquisition already toggles the display. Its successful hardware
results are summarized above; document reuse remains disabled.

The desktop detects `acquisition_settings_version=2` on connection. Configuration
is read and verified at connection/Apply. Each frame sends one `ACQUIRE_GUARDED`
request with `settings_mode=managed` and that verified recipe; there is no
standalone settings query and no per-frame COM query for exposure, accumulations,
geometry, timing, ADC or gain. The bridge starts the acquisition directly, retains
fresh temperature checks before/after exposure and reads the already-saved SPE.
Frame dimensions, exposure, accumulation count and ADC/gain are verified from
the SPE header before accepting the frame. Missing or inconsistent frame evidence
rejects the frame; accumulation normalization uses the acquired header count.
Full settings/legacy ROI-object discovery remain available at connection and Apply.
Older bridges retain the original full-settings acquisition protocol.
`GET_ACQUISITION_SETTINGS` is also available for explicit read-only diagnostics.
Per-frame LightField checks read only the selected grating, center and output;
capability/choice discovery occurs during Apply, rather than on every spectrum.

Managed acquisition assumes SpectralSweep owns the recipe throughout the run:
do not edit camera settings in the WinSpec UI during measurement. Actual SPE
exposure/accumulation/shape/gain changes are detected on the returned frame;
detector identity, ROI enable and timing configuration are cached from Apply,
not represented as fresh observations. Metadata `settings_scope=configured_plus_spe`
and `settings_provenance` distinguish the two sources. The header layout follows
[WinSpec/32 manual Appendix C](https://www.afs.enea.it/apruzzes/Pdf/WinSpec32.pdf):
exposure seconds at offset 10, `NumExpAccums` at 1422, with legacy `lavgexp` at 668
used only when the modern accumulation field is zero. Invalid counts fail closed.

Frame metadata contains `host_timing_s` (settings, optics before/after, acquisition
request, processing, total), `bridge_timing_s` (prepare, document creation/start, exposure
wait, post-exposure check, SPE save/read, final settings and total before transfer),
and `client_timing_s` (request total including receipt, cleanup wait). Bridge phase
timings also appear in `GET_LOGS`. Existing `elapsed_s` retains its earlier meaning:
it excludes final settings reads and transfer/cleanup. Timings are diagnostic;
exposure, averaging, temperature policy and data-recovery behavior are unchanged.

To activate, make sure WinSpec is idle, close only the XP camera **server** window,
and leave WinSpec/cooling running. Measurements using other detectors can continue.
In the staged `XP_TRANSFER\startup-performance-v11`
folder run `update_performance_bridge.cmd`, restart
`C:\WinSpecRemote\start_camera_server.bat`, and verify the banner contains
`2026-10-01-startup-performance-v11`. This bridge-only update does not require a
desktop restart. Load pending desktop code changes after any current measurement
ends. The updater
backs up the previous bridge and temperature guard in `C:\WinSpecRemote`.
No improvement is claimed until the deployed bridge is verified and comparable
measurements are timed.

This backend borrows the application's connected LightField spectrograph and
uses the existing XP WinSpec TCP bridge for the detector. It does not launch
another LightField Automation instance. Spectrum supports raw 1–512 pixel
acquisition; Presets and MegaSweep use matching saved WinSpec wavelength
calibrations through the calibrated scan adapter.

## Connect and acquire

1. Start the XP VM, connect the Roper USB device to XP and start the existing
   WinSpec camera server on port 5000. Select full 512 × 1 readout, no ROI,
   and Free Run timing in WinSpec. Finish any WinSpec acquisition first.
2. Restart SpectralSweep to load these code changes when existing work is idle.
3. In Instruments, uncheck **Use mock**, connect **LightField**, and load its
   normal spectrograph experiment. Keep this connection open.
4. Choose **WinSpec InGaAs + LightField spectrograph**. The XP host
   defaults to `192.168.170.128` and port `5000`; connect without retyping them.
   Edited addresses are saved when connecting.
5. In Spectrum, select the desired grating, center wavelength, exposure and
   accumulation count. **Apply** / **Acquire 1D** sets the center wavelength,
   selects the LightField side output, and configures WinSpec exposure and
   accumulations (one sequential output frame). Acquisition returns WinSpec
   detector counts, not the LightField camera image.
6. Save the spectrum or add it to references. CSV uses `pixel,intensity_counts`;
   metadata records the uncalibrated pixel axis. Pixel references are hidden
   when viewing calibrated nm spectra and vice versa.

Disconnecting this backend leaves the borrowed LightField session and XP
camera cooling running. Disconnecting LightField also removes its dependent
WinSpec connection. After aborting, Apply/Acquire rearms the detector client.

## Calibration and limitations

The camera model is provisionally OMA V 512. The configured 50 µm pitch is
unverified and is not used to calculate wavelength. The existing 1340-pixel
silicon front-port calibration must not be applied to this 512-pixel detector.
Wavelength-based scans require a separately validated WinSpec calibration.
Before Presets or MegaSweep commands gate voltages, every planned center is
checked against saved calibration coverage and the live grating, output,
detector and setup profile. Missing coverage blocks the scan with the center
and grating in the error. Each acquisition also verifies that its optical
context stayed unchanged. Scan CSV headers contain calibrated nm values and
counts are restricted to the matching calibrated pixels. The experiment's
capture metadata stores the frozen calibration record and wavelength axis;
the detector inventory still describes the underlying raw pixel detector.
Spectrum's raw acquisition remains available for collecting calibration data.

## WinSpec wavelength calibration window

Spectrum → **WinSpec wavelength calibration…** opens a separate modeless window.
This feature only processes WinSpec frames and saves its own configuration records;
it never calls IntelliCal or writes/clears LightField/PIXIS calibration settings.

1. Select idle WinSpec and set the desired grating and center in Spectrum.
2. Mount the USB-Hg_NeAr lamp and select Ne/Ar. In the calibration window choose
   **Acquire lamp spectrum using Spectrum settings**. Acquisition uses the existing
   LightField connection for optics and XP bridge for counts. Current hardware
   identity and optics are checked before/after the frame. Missing or changing
   readbacks prevent calibration.
3. Inspect the raw plot. Candidate peaks have subpixel parabolic positions but
   no automatic wavelength assignments. Choose known wavelengths from the extracted
   LightField **NIR PI Neon** list, or enter verified values. Reject blends and
   saturation. Assign **Fit** to at least 3 lines for a linear fit (4 for quadratic)
   and **Check** to at least one independent line inside the fitted interval.
4. Confirm the source/detector checkbox, click **Fit and validate**, inspect the
   RMS and independent residuals, then **Save WinSpec calibration**. The default
   maximum residual is 0.2 nm; this is an acceptance threshold, not a manufacturer
   accuracy guarantee. A good residual cannot prove a guessed line identity.
5. The next matching WinSpec acquisition displays nm within the fitted pixel
   interval only. Full raw counts and calibration evidence remain in export metadata.
   Original pixel order is preserved even for decreasing wavelengths; counts are
   never reversed independently from their coordinates.

Each grating, center wavelength, output, geometry and setup profile needs its own
record. Exact context mismatch falls back to pixels, without wavelength extrapolation.
Changing center requires a new local fit; this is not a broad spectrograph model.
The application reads the spectrograph serial, but the XP bridge does not report
a verified detector serial. Confirm the same physical detector/mounting when enabling
**Apply saved WinSpec nm calibration** each application session. Recalibrate after
remounting or optical adjustments. PIXIS continues to use its existing LightField axis.

## WinSpec broadband capture and validation

Restart the app to load the calibration controls. Select WinSpec, then open
**WinSpec calibration → Broad calibration: 900–1700 nm**. With the Ne/Ar source
confirmed, **Collect 900–1700 nm automatically (50 nm steps)** collects 17 center
positions using the current grating and existing acquisition settings. Raw JSON
and CSV files are saved under `%APPDATA%/SpectralSweep/winspec-calibration-captures`.
Closing the calibration dialog or clicking Stop prevents further centers; the
current exposure may finish. The spectrograph remains at its last center.

Collection alone is not calibration. Reference-line identities and local fits
must be checked before building a broad model. Anchor centers and independent
check centers must be distinct. The empirical model interpolates only between
validated anchors, within their shared fitted pixel range; unchecked intervals
remain pixels. A 900–1700 nm center scan does not guarantee wavelength coverage
throughout that range. Repeat per grating, with closer spacing if checks fail.
Records are separate from PIXIS/LightField IntelliCal and do not modify it.

## Multiple optical setups (routing)

In Instruments, select or type an **Optical setup profile** before connecting.
The default profile routes the LightField camera (PIXIS) to Front and WinSpec
to Side. Each detector's exit is configurable independently. New profile names
and route choices are saved when connecting. Disconnect LightField and WinSpec
to edit or change the profile; switching between connected detectors preserves
the profile. Profiles describe physical wiring, not wavelength calibrations.

For a spectrograph with a physically fixed front exit, choose **Fixed front exit**.
This uses no exit switching commands and marks WinSpec **Not installed**. If
the WinSpec detector is actually installed at a fixed exit, create a corresponding
profile and assign its physical exit explicitly. An SDK read failure is never
accepted as proof of fixed hardware. A missing exit setting requires an explicit
fixed-exit profile; contradictory capabilities or unsupported Side exits fail.

Before LightField camera acquisition, the camera's configured exit is restored
and verified; thus acquiring PIXIS after WinSpec returns to Front. WinSpec
configuration selects its own route and acquisition verifies it again. Single
and fixed-exit devices do not show an exit-switch control in Spectrum.

Clicking **Use selected** for an already connected detector now also applies
and verifies its exit immediately, before reporting that detector active.
Connecting WinSpec applies its route on the borrowed LightField session.
A routing failure keeps the previous device selected and reports an error;
verify the physical exit if a write succeeded but its readback failed.

InGaAs data and exports retain the original 1–512 pixel/count pairs. The Spectrum
checkbox **WinSpec: reverse pixel display (short wavelength on left)** defaults on
for this setup, whose direction was measured by moving the spectrograph center.
It reverses the pixel view (512 on the left, 1 on the right), labels it
**Pixel (reversed; uncalibrated)**, and persists the preference. It does not alter
raw data, local calibration peak numbers, or PIXIS displays. Calibrated nm axes
always increase to the right, including when the stored wavelength array decreases.
Other physical WinSpec arrangements may require disabling this preference.

Development validation used mock optics/camera acquisitions and a real read-only
GET_SETTINGS probe (512 × 1, −100 °C, locked, idle). Live side-port motion and
camera acquisition still require an end-to-end check in the restarted app.
Focused suites pass in separate processes; combining the existing Spectrum
panel and threaded controller suites triggers a Windows Qt access violation,
including when the new WinSpec tests are excluded.

## InGaAs temperature interlock

The WinSpec adapter requires actual temperature **<= -100 C** and a true locked
readback. No tolerance is added. Missing, nonnumeric, nonfinite or unlocked
readbacks block acquisition. Configure and direct acquire both check; a failed
acquire latches abort until an explicit new Apply/Acquire request. PIXIS is unaffected.

Deploy both `tools/winspec/camera_server.py` and `temperature_guard.py` beside the
XP bridge launcher and restart the bridge, then restart SpectralSweep. Old bridges
cannot satisfy ACQUIRE_GUARDED and the new desktop adapter blocks them. The source
files alone do not protect an already running application.

The XP bridge polls every 0.5 s in its own COM apartment during exposure. A trip
attempts Stop and rejects the frame. An independent watchdog attempts Stop when
readbacks are stale for >3 s. Pending COM monitor/Stop work locks out further
acquisitions until bridge restart. It never writes cooling settings. Polling cannot
exclude shorter excursions; stopping depends on WinSpec responding to COM. A
hardware/driver hang cannot be solved by a software interlock.

Accepted spectra carry the guard report (limit, sample count, min/max, timestamp,
largest gap) in frame metadata. UI acquisition errors stop continuous acquisition
and broad calibration collection and display the reason. No automatic resumption.

The bridge still needs a real guarded exposure check after deployment: confirm
that this WinSpec build supplies temperature during WaitForExperiment. If it does
not, acquisition fails closed; do not disable the checks to work around it.

## Shared LightField window

PIXIS and WinSpec share one SpectralSweep-owned LightField Automation session.
An empty experiment is a connected session, although it is not ready to acquire:
load the appropriate experiment in that existing window. Connection no longer
requires acquisition capabilities within 15 seconds and therefore does not
discard the window and spawn another on retry. Measurement preflight still
checks readiness. Disconnect/shutdown disposes the owned Automation instance;
switching to/from WinSpec parks and reuses it instead.


### Serialized acquisition bridge (2026-09-27)
Build `2026-09-27-serialized-acquisition-v4` retains temperature guard protocol 3
(cold_or_locked). Routine acquisition and temperature COM calls use one worker;
DM_LASTFRAMERDY supplies completion evidence instead of blocking
WaitForExperiment while another thread polls temperature. An independent watchdog
still attempts Stop on stale monitoring or timeout. Incomplete captures are rejected.
Successful SPE files and their WinSpec windows are retained on XP; the bridge does
not call DocFile.Close (which can open a save prompt). No manual Save As is needed
for bridge acquisition. Existing manual/old modal dialogs must be resolved before
starting the updated bridge. This fixes suspected software interaction; resolving
USB Data Overrun still requires repeated hardware verification after deployment.
