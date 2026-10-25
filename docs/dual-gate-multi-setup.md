# Dual Gate with multiple measurement setups

Connect **LightField + PIXIS / CCD** first, then **WinSpec InGaAs + LightField
spectrograph** in the instrument panel. Keep both sessions connected. The scan
selects these connections and shares the existing LightField spectrograph.

The sidebar's active setup controls Spectrum and plans without a Measurement
setup row. A Dual Gate plan with that row selects its own setup at each recipe;
you do not need to manually select PIXIS first. During a scan the sidebar
follows the active setup after each successful activation and shows its
configured exit. Setup/connect/disconnect controls remain locked throughout
measurement. Updating this display does not reconnect devices or schedule an
optics refresh. A failed activation retains the previous active setup; after
measurement the sidebar stays on the last setup used and becomes editable.

In the existing **Dual Gate** loop table, add a row and select **Measurement
setup** in the Parameter dropdown. Click its **Values** cell to open the setup
selector. Choose **PIXIS**, **WinSpec**, **Andor InGaAs** or **Andor Silicon**
for each position using the dropdowns; use
**Add setup**, **Up**, **Down** and **Remove** to edit the sequence. Repeated
setups are allowed. **OK** updates the draft; **Cancel** leaves the table unchanged.
Wavelength, exposure and EPF Values remain ordinary editable numeric lists.

Connect each requested setup in the instrument panel before running. Andor
Silicon uses **Andor Shamrock + Si CCD**; Andor InGaAs uses **Andor Shamrock +
InGaAs CCD**. The existing shared-Shamrock connection allows one Andor role at
a time, alongside LightField/WinSpec. Use one Andor role per run; disconnect
the existing Andor camera before connecting the other role. The same Andor
choices are available in the 2D-map optical-condition setup dropdown.

With **Settings > MFF101 light paths > Automatically route the selected
measurement setup** enabled, the scan routes the flipper before preparing and
acquiring each setup: **Andor Silicon / InGaAs → position 1** and
**LightField PIXIS / WinSpec → position 2** by default. Edit the setup-position
dropdowns in Settings when the physical routing changes. This applies to
complete sweeps, setups nested inside gate points, and 2D-map optical conditions.
Initial recipe preflight also visits each requested setup. Acquisition waits
for verified arrival; a routing failure stops the scan before the next setup
can acquire. Editing/restoring the draft does not move hardware, and simulated
setups do not move the physical flipper.

Select a loop row and use **Up**, **Down**, **Top** or **Bottom** to move the
complete row. Its enabled state, parameter, raw Values and Group move together.
The current execution nesting is retained, including in Synchronize mode;
change actual nesting in **Execution order**. Reordering is a draft change that
can be applied, discarded or saved with the session.

For a paired setup sequence, select **Customized** mode, add and enable a
**Measurement setup** row, and enable and edit the center, exposure and EPF
rows. Assign the same Group to these rows and enter one value per setup,
for example:

| Parameter | Values | Group |
| --- | --- | --- |
| Measurement setup | PIXIS, WinSpec | 1 |
| Center Wavelength (nm) | 730, 1150 | 1 |
| Exposure Time (ms) | 100, 500 | 1 |
| Accumulations (EPF) | 2, 3 | 1 |

The first value in each row belongs to PIXIS and the second to WinSpec.
Rows in the same Group pair by position and must have equal value counts.
Setup, center, exposure and EPF may also use **different Groups**: those groups
form all combinations. For example, setup Values `PIXIS, WinSpec` in Group 1
and center Values `730, 1100` in Group 2 produce four recipes, including
PIXIS at 1100 nm and WinSpec at 730 nm. Zip pairs all enabled rows; Synchronize
gives each enabled row its own loop. Disable a numeric loop row to use its
global value for all recipes. Use positive exposure/center values and positive
integer EPF values. Actual wavelength availability depends on the instruments
and saved calibration. WinSpec needs a calibration covering each requested
center, grating and detector configuration before the gates can move.

Add more positions in the setup selector to repeat either detector at other wavelengths:
`PIXIS, PIXIS, WinSpec` can pair with numeric Values `720, 730, 1150` and three
different exposure times. Existing saved text-based setup lists still load;
unrecognized setup names must be selected again before the editor accepts them.
Rotation/stage loops remain independently editable in other
groups. Batch gate voltages, point counts, repetitions, conditions and timing
remain editable as before. **Exposure Time** is the integration time per exposure;
**EPF** is the device accumulation count; batch **frames** is the gate-point count.

Review **Measurement sequence preview**, then click **Apply plan** before **Run**.
Editing the sequence does not start a measurement or apply hardware settings.
Clicking **Run** starts a preparation phase on the existing run worker. The
status/log distinguish plan validation, durable plan saving, and instrument
preparation, with validation/save elapsed times. Full schedule checks, output
directory inventory, JSON encoding and history updates do not run on the GUI
thread. The current applied plan and filename inputs are captured for the run;
edits remain a separate draft until it finishes. Stop remains responsive during
preparation: it waits for the current validation/save to finish, then cancels
before any instrument command. Failed plan persistence also blocks instrument
preparation. A cancelled startup does not send an unnecessary zero ramp.
The inline preview shows at most eight nearby steps, together with the full
plan counts. **Full sequence** shows up to 200 steps per page. Use **Previous**,
**Next** or the page number to inspect another part of the plan; manual paging
turns off **Follow current step**, so live progress does not move your page.
Enable it again to return to the active acquisition. Paging only changes the
display: every planned acquisition still runs and is recorded.

Metadata is saved at file boundaries. Before each file's first acquisition,
actual settings and full-precision calibration are persisted, followed by a
`file_started` event in `.experiment.events.jsonl`. Unchanged points write their
scientific data to CSV without reading a metadata snapshot or appending capture
events. Each successful spectrometer reconfiguration or backend change invalidates
the settings snapshot; an open file then receives `file_settings_changed` before
its next acquisition. WinSpec snapshots retain the scan adapter's complete
calibration record and wavelength axis.

After a CSV closes successfully, a synchronous `file_completed` journal event
saves its file/settings IDs, start/end time, successful point count and CSV row
count. A full-sensor point can span several CSV rows. Stop/error progress is
recorded immediately before file closing and hardware cleanup, followed by
`file_stopped` or `file_failed`; close errors never produce `file_completed`.
No per-point metadata queue is retained in RAM. A crash may leave an unfinished
file without its terminal summary; its previously written CSV and durable file
start/settings remain available. The final manifest adds output associations
and `summary.completed_points`; individual capture-attempt counts and per-point
timing/temperature history are not recorded. Keep the journal with the manifest.
If you change group membership, use **Reset order**, review the order,
and apply the plan again, as with other Dual Gate loop edits.

**Detector wavelength reminders** appear below the loop table for the resolved
measurement combinations, including any batch `When` filtering. The application
recognizes PIXIS as Silicon CCD and WinSpec as InGaAs from the selected setup.
It reminds you when PIXIS centers exceed 1000 nm (for example, 1100 nm), or
InGaAs centers are below 1000 nm (including visible centers such as 730 nm).
Without a setup row it uses the connected detector; without a center row it
uses the global center. These are advisory thresholds, not measured detector
cutoffs. Exact response depends on the detector variant; a wavelength
calibration does not establish sensitivity. The
[manufacturer's PIXIS specifications](https://www.teledynevisionsolutions.com/products/pixis/)
describe family coverage up to approximately 1100 nm, with sensor-dependent QE.

Reminders update as you edit and do not invalidate the plan or change the
selected setup. **Run** presents one combined reminder before recording the run
or starting measurement. Choose **Continue measurement** to proceed or **Cancel**
to return to editing. Invalid or oversized drafts explicitly show that the
detector check is pending until the draft is resolved/applied.

By default the paired optical group is outermost: complete the PIXIS sweep,
return the SMU channels to zero, then switch setup and repeat the sweep with
WinSpec. A failed zero return blocks the next setup. Placing the paired group
inside **Gate points** instead measures both recipes at each gate point, keeping
that gate voltage while switching optics. This increases optical switching.

Every distinct recipe is configured/validated before the first gate write.
LightField applies the configured exit before setting the center wavelength.
It checks the SDK's `IsRunning` and `IsUpdating` flags and requires the exit,
grating and finite center readbacks to remain unchanged for at least 300 ms
while the center setting is available/writable and the experiment is idle.
This also catches delayed exit/calibration updates after the SDK first reports
Ready. `IsReadyToRun` is recorded separately: an external WinSpec detector does
not require LightField's own camera to be ready to acquire. Setup preparation
errors identify the requested setup, active setup and configured exit, as well
as the underlying setting error.
The center must read back within 0.01 nm of the request for three consecutive
ready/idle checks. Exit settling, center verification and recovery share the
existing 15-second optical timeout; readiness or diagnostic queries do not
restart it. Synchronous SDK calls cannot be interrupted by this timeout, but
an expired deadline blocks further writes and prevents reporting success.
This tolerance
checks SDK setting agreement; it is not a claim of calibration accuracy.
If a matching center subsequently reverts, preparation may rewrite that center
at most twice, after another 300 ms settling check. Recovery requires complete
initial exit/grating readbacks and aborts if either changes. A center that never
matches, or becomes unavailable/nonfinite, is not automatically rewritten.
The WinSpec scan preflight selects its configured exit before looking up the
exit-specific wavelength calibration, within the same optical timeout.
Exposure time and EPF must also match their readbacks. A missing or mismatched
readback stops preparation instead of recording a successful setup change.

Each PIXIS capture checks the exit, center, exposure and EPF before and after
acquisition, together with the grating selected during setup (when available).
The SDK wavelength axis is refreshed for each acquired spectrum; changing the
axis within a CSV stops export before appending mismatched data.
These checks do not move the exit mirror. A changed setting blocks
acquisition or discards the affected frame before CSV export; earlier verified
rows remain saved. Errors report the requested and observed settings, and the
file journal records the failed file and active point context.
An exit mismatch requires applying the setup again. Existing CSVs are not
relabelled or repaired by these checks.
Once validation fails, manually restoring a device value does not resume
capture: apply and verify the settings again. The 2D scan uses the same strict
axis check when saving each frame and propagates acquisition failures instead
of appending placeholder NaN spectra. Previously completed rows remain saved.

While Dual Gate or another scan owns the spectrometer, Spectrum Apply, Acquire
and Run controls are disabled. The controller also rejects these requests,
including queued requests dispatched before the scan took ownership. Spectrum
controls become available after the scan releases ownership.

Both legacy and managed WinSpec connections compare exposure time, EPF and
timing against the verified configuration. Legacy connections check live
settings before acquisition; both paths check returned frame settings before
publishing data. A frame cannot replace the expected configuration. Failed
configuration or acquisition validation requires a successful Apply before
continuing; Dual Gate records the error in its interrupted-run and file summaries.

With optical parameters in separate groups, the complete recipe is still
configured atomically at the Measurement setup group's position; group nesting
determines the order of measurement combinations.
Existing acquisition temperature checks and failure handling remain active.
Files carry a PIXIS/WinSpec suffix and use separate wavelength axes; saved plans
and acquisition metadata record the selected setup and optical settings.
Plans without a Measurement setup row continue using the current detector.

Real LightField connections also save center-write diagnostics in
`%APPDATA%/SpectralSweep/diagnostics/lightfield-center-<process-id>.jsonl`.
Each Spectrum Apply or scan center write records its source, requested center,
SDK state before/after the call, state just before SetValue, actual exit/grating,
IsRelevant/IsValid and allowed range when available, and up to 32 readback
changes. For recovery, the record preserves the first pre-write context and
each subsequent write context, rewrite reason, readback and settling evidence.
Diagnostic queries themselves do not write settings or relax acquisition
checks. A log-write failure cannot replace the original
setting outcome. Diagnostic records help compare manual success against an
automatic setup failure; they are not evidence that an unresolved hardware
failure has been repaired.

Changes require restarting SpectralSweep. Automated verification uses simulated
instruments; validate the selected optical route and acquisition on the actual
instruments before a production measurement.

## Choosing values for the current run

Keep the complete arrays in **Values**. Use **This run** to choose All values,
one value, or Select several. Selecting WinSpec in a paired setup/wavelength
group also selects its matching wavelength; returning to All restores both
setups without editing either array. Independent exposure-time groups can be
selected separately. Apply plan commits the choice before Run.

Selections follow the active loop mode: independent rows in Synchronize,
all enabled rows together in Zip, and shared Group numbers in Customized.
The complete enabled arrays are validated before selection, so selecting one
position cannot hide unequal paired array lengths. Disabled rows keep their
arrays and selections. Selection does not change execution nesting or activate
the point scheduler for a legacy session.

All automatically includes newly added values. A specific selection requires
choosing again after the parsed array values or paired row membership change;
the selector displays **Select again…** and Apply/Run remain blocked. Renaming
a group or moving a complete row preserves its selection. Temporary invalid
text stays editable and can be corrected or discarded. Repeated equal values
remain separate occurrences; the picker shows position suffixes to distinguish
them. Large arrays use the searchable multi-selection picker rather than
expanding every value in the dropdown.

Sessions save the complete draft and applied arrays and their respective
selections. Older sessions default to All. Experiment metadata records the full
definition, selected positions, and resolved execution plan. Run freezes the
applied plan; draft edits cannot replace an active schedule. Readiness and
detector reminders use the selected schedule, including When filters; a plan
with no runnable acquisitions cannot run. Tab moves from Values through This
run to the Group editor in Customized mode.
