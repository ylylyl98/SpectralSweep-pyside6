# Multi-setup 2D Sweep

Connect LightField/PIXIS first, then WinSpec using the same LightField spectrograph.
Keep both sessions connected. WinSpec needs a matching saved wavelength calibration.
The sequence only selects existing connections; it never starts a second LightField instance.

In 2D Sweep, click **Load PIXIS + WinSpec recipe** in the optical condition editor.
This replaces only the optical rows, leaving the voltage grid and timing intact:

| Setup | Center | Exposure per frame | Frames | Combine |
| --- | --- | --- | --- | --- |
| LightField + PIXIS | 650 nm | 80 ms | 4 | Average |
| LightField + WinSpec | 1050 nm | 250 ms | 4 | Device EPF |

Add or duplicate rows for further maps. Each enabled row produces a separate full map
over the same gate points, with setup and the combination/count included in filenames. Old saved rows
default to **Current setup / Device EPF** and retain their original behavior.

**Average** sets device EPF/accumulations to one and takes four independent frames at
each voltage point, storing their arithmetic mean. It does not divide an undocumented
device sum. The setup, count, reduction mode and requested settings are saved in metadata.

WinSpec defaults to **Device EPF**: four internal accumulations return one frame
per gate point, and SpectralSweep divides the acquired sum by the verified
accumulation count to store mean counts per exposure. This reduces repeated
document/file/transfer overhead. **Average** remains selectable; an explicitly
saved Average recipe is restored as saved. PIXIS retains Average in this recipe.

Before any gate writes, every selected recipe is configured and Average rows receive one
single-exposure validation capture (not saved as a map point or included in the average).
This verifies calibration and actual spectral geometry, including trimmed wavelength axes.
PIXIS must return a one-dimensional spectrum; full-sensor images are rejected with a
request to select spectral/vertical-binned readout. No spatial reduction is silently applied.

The scan completes the PIXIS map, returns the SMUs to zero, then configures WinSpec and
repeats the grid. A failed zero return blocks the next map. Device/calibration/acquisition
failures stop the sequence and retain existing failure handling. Stop is checked between
frames and discards any incomplete average. Background temperature polling remains paused
for the entire sequence; detector acquisition temperature interlocks stay active.

Changes take effect on the next app launch. Hardware operation still requires validation
on the connected instruments; automated tests use simulated devices only.

WinSpec previous-frame diagnostics (elapsed time and temperature-guard observations)
are recorded as `previous_frame_observation` in capture-start events, not as instrument
settings. Unchanged settings and calibration therefore share one settings snapshot;
real settings changes still create a new snapshot. Events remain append-only and the
summary is checkpointed by the existing metadata service.

After ten measured point intervals, the status shows the current map's remaining time
using up to the latest 100 intervals. Timing resets at each map/setup change; it does not
extrapolate PIXIS speed to a future WinSpec map. The pre-run estimate remains nominal.
