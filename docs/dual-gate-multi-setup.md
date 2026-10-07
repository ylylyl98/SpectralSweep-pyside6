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
selector. Choose PIXIS or WinSpec for each position using the dropdowns; use
**Add setup**, **Up**, **Down** and **Remove** to edit the sequence. Repeated
setups are allowed. **OK** updates the draft; **Cancel** leaves the table unchanged.
Wavelength, exposure and EPF Values remain ordinary editable numeric lists.

Select a loop row and use **Up**, **Down**, **Top** or **Bottom** to move the
complete row. Its enabled state, parameter, raw Values and Group move together.
The current execution nesting is retained, including in Synchronize mode;
change actual nesting in **Execution order**. Reordering is a draft change that
can be applied, discarded or saved with the session.

Alternatively, click **Load PIXIS + WinSpec recipe** under the loop table.
This loads an editable draft and selects **Customized** mode. It replaces the
optical loop rows while preserving gate rows, motion values and motion nesting.
The initial centers are 720 and 1100 nm; they are examples, not fixed limits.
Exposure and EPF initially use the current settings for both detectors.

Edit the Values cells, for example:

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
Loading a recipe does not start a measurement or apply hardware settings.
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
acquisition journal records capture validation failures when metadata is active.
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
continuing; failed captures are recorded in the acquisition journal.

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
