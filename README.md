# SpectralSweep

SpectralSweep is a PySide6 desktop application for spectra acquisition and sweep-driven optical measurement workflows. The repository is organized around the current desktop UI and its instrument-control runtime.

The application is intended for lab setups that combine either Princeton Instruments LightField or an Andor SDK2 camera with a Shamrock spectrograph, voltage control, motion control, and optional optical power measurements. The desktop UI keeps instrument connections, sweep setup, live preview, and data capture in one operator-facing workflow.

## Main Features

- PySide6 desktop interface with a docked instrument-control panel and dedicated workflow tabs
- Live spectrum viewing with selectable LightField, Andor Si CCD, or Andor InGaAs CCD backends
- Presets-based spectra sweep planning with loop tables, batch conditions, and CSV export
- MegaSweep voltage-mapping workflow for Vtg/Vbg and D/Vbias acquisition patterns
- BFP viewing and export tools
- Hardware-controller wrappers for LightField/Andor, Keithley SMU workflows, rotation stages, linear stage, and Thorlabs PM100D
- Mock spectrum mode for UI development without live spectrometer hardware

## LightField experiment metadata

LightField runs in Presets, Power Sweep, MegaSweep, BFP, and both MCD tabs
automatically read instrument settings immediately before each capture. The
experiment's `*.experiment.metadata.json` stores distinct configurations under
`observed.lightfield.settings_snapshots`, separate from requested UI settings.
Readbacks include exposure, exposures per frame, combination method, stored
frames, grating label, center wavelength, ports/slits, readout/gain, detector
temperature setpoint, ROI/binning, correction flags, device identity, experiment
name, application assembly version, and detector wavelength calibration.
Unsupported or failed reads are `null` with a reason in `unavailable`.

The adjacent `*.experiment.lightfield.jsonl` contains capture start/completion
or failure events, linked by `acquisition_index` and `settings_id`. Start events
include actual detector temperature, the number of frames requested from Capture,
and sweep context (output file/point where available; angle for MCD2100).
BFP warmups are labeled explicitly. Capture indices include failed attempts and
warmups and are **not CSV row numbers**. A start without a completion can indicate
interruption; capture completion does not itself mean an output row was saved.
The journal is registered in the experiment's file list; keep it with the JSON
and data files when copying a run.

The grating label is preserved exactly as LightField reports it; groove density
is not inferred from an index. Calibration in the JSON is the SDK's detector
axis before alignment/binning; the exported spectrum's wavelength column is the
authoritative saved axis. Optional metadata failures are logged without stopping
acquisition. This metadata is provenance and is not automatically restored to
hardware when loading saved settings.

## Power-meter correction

The PM100D Power Meter section has one shared **Power correction factor**:
corrected sample power = raw meter power × factor. The sidebar readout shows
corrected power; its tooltip includes the raw reading and applied factor.
Motion Sweep and Presets meter measurements capture the factor at run start,
so editing it during a run only affects later runs and standalone readings.
Their CSV files preserve `Power_raw_uW`, `Power_correction_factor`, and
`Power_uW` (corrected power); experiment settings also record the run factor.

Manually entered power is sample power in µW and is used directly. Filename
generation never multiplies power again. The former filename coefficient is
migrated to the shared meter setting when loading a config without a PM100D
factor. Legacy per-panel coefficient controls are no longer shown or applied.

## Supported Launch Path

The only supported application entrypoint is:

```bash
python main.py
```

For Windows lab machines, the supported launcher is:

```bat
launch.bat
```

To force mock LF6 mode:

```bash
python main.py --mock
```

## Installation

1. Create and activate a Python 3.11-3.13 virtual environment.
2. Install the desktop-app dependencies:

```bash
pip install -r requirements.txt
```

3. Ensure vendor hardware software is installed as needed for your lab setup:
   Princeton Instruments LightField for LF6 automation; or Andor SDK2 and Shamrock DLLs for the Andor backend; plus VISA support for Keithley / Newport communication and Thorlabs PM100D driver files where applicable.

## Andor Si / InGaAs Setup

1. The repository-bundled `andor dll` and `andor dll/shamrock dll`
   directories are selected automatically. Override them on the Settings tab
   only when using another installed Andor runtime.
2. Set the Si and InGaAs camera indices. A camera serial number is strongly
   recommended; when provided, it takes precedence over the index and prevents
   the wrong detector from being selected if Windows changes enumeration order.
3. Configure the Shamrock index and shared optical defaults. Si and InGaAs
   cooling targets, cooler-on-connect choices, and fan modes are stored as
   separate detector profiles.
4. In the instrument sidebar under Spectrum Detector, choose **Andor Shamrock
   + Si CCD** or **Andor Shamrock + InGaAs CCD** and connect. Both choices use
   the Shamrock spectrometer; the choice determines which attached detector is
   opened. The selected pair is shared by Spectrum, Dual Gate, 2D Sweep,
   Motion Sweep, MCD, MCD 2100, and BFP acquisitions.

After a real Andor pair connects, expand **Andor controls** below the plot in the
Spectrum tab. The drawer shows grating groove/blaze information, center
wavelength, input slit, shutter, Shamrock output port, cooling, and the active
stored wavelength calibration. For the Si CCD it also exposes 2D ROI and
horizontal/vertical binning. **Apply + verify** writes the displayed operating
controls and reads them back before reporting success. The Instruments sidebar
remains focused on connection, detector temperature, and safe warm disconnect.

The Spectrum tab supports one-shot **Acquire 1D/2D** and continuous **Run 1D/2D**.
Continuous capture is sequential: the next request starts only after the prior
frame arrives. The one-dimensional InGaAs array exposes only the 1D actions;
the Si CCD supports both 1D spectra and 2D detector frames.

Wavelength arrays come exclusively from calibration coefficients already stored
in the Shamrock controller. SpectralSweep supplies the selected detector's pixel
geometry and reads `get_calibration()`; it never fits, resets, or overwrites
calibration coefficients. Results are cached until detector geometry, read mode,
grating, or center wavelength changes.

The cooler is disabled on connection by default. Use **Warm up + disconnect**
for a cold detector; it turns cooling off and waits for the configured safe
temperature before closing the SDK connection. Before production data, verify
the optional wavelength-axis reversal against a known spectral line without
changing the stored calibration.

## Project Structure

```text
SpectralSweep-pyside6/
|-- app/
|   |-- devices/
|   `-- engine/
|-- controllers/
|-- ui/
|-- utils/
|-- iv_automation.py
|-- lf6_automation.py
|-- launch.bat
|-- main.py
|-- requirements.txt
|-- TLPMX.py
`-- TLPMX_64.dll
```

## Folder Guide

- `app/`
  Shared runtime pieces used by the desktop UI: hardware adapters and CSV writing.
- `controllers/`
  Qt-facing controller layer that owns live instrument connections and exposes them to the UI panels.
- `ui/`
  PySide6 widgets, tabs, and the main application window.
- `utils/`
  Non-UI support code such as persistent config handling and LF6 mocking.

## Main Runtime Modules

- `main.py`
  Desktop entrypoint that initializes Qt and opens the main window.
- `ui/main_window.py`
  Builds the application shell and wires all instrument controllers into the tabbed UI.
- `ui/presets_panel.py`
  Presets-driven spectra sweep workflow and CSV acquisition runner.
- `ui/megasweep_panel.py`
  Voltage sweep planning, live path preview, and measurement export.
- `controllers/lf6_controller.py`
  Shared LightField / Andor connection and acquisition control (legacy module
  name retained for compatibility).
- `controllers/smu_controller.py`
  Keithley / IV workflow integration used by sweep panels.

## Hardware Notes

Some modules depend on lab-specific hardware and vendor runtimes:

- `lf6_automation.py` integrates with Princeton Instruments LightField through `pythonnet`.
- `app/devices/andor_adapter.py` integrates Andor SDK2 cameras and Shamrock
  spectrographs through `pylablib`, with all SDK operations serialized on one
  owner thread.
- `iv_automation.py` uses VISA and NI-DAQ related interfaces for supported measurement workflows.
- `TLPMX.py` and its bundled DLL support Thorlabs PM100D discovery and readout.
- Motion-stage adapters under `app/devices/` rely on the corresponding device libraries and connection paths available on the host machine.

If you are working on the UI without hardware access, start with `python main.py --mock`.

## Remembered Setup

The application automatically restores the last selected workflow tab, window
layout, instrument connection choices, and editable setup fields for Dual Gate,
2D Sweep, Motion Sweep, BFP, Spectrum, and Settings. Dual Gate keeps the edited
draft and the last applied tables separately. Sample ID is shared by all
measurement tabs, so editing it in one workflow immediately updates the others.

Motion Sweep can use the linear stage, Rot1, or Rot2. The PM100D is optional;
when it is disconnected, spectra are still acquired and the output omits
optical-power values.

Live connections, voltage or motion targets, polling, acquired data, plots,
progress, and logs are deliberately not restored. Connecting instruments and
starting or applying a run always remains a manual action.

On Windows, configuration is stored under
`%APPDATA%\SpectralSweep\config.json`. An existing repository-level
`config.json` is imported as a compatibility default and is left untouched.
Writes are debounced and atomic.

## SMU Hardware Incident Reports

Keithley communication uses a finite VISA timeout. If an SMU stops responding,
the Dual Gate runner records the role, VISA address, frame, failed command,
recent SMU operations, read-only post-failure diagnostics, traceback, and
per-role zero-ramp result in `hardware_incidents.jsonl` beside the run CSVs.

For a responding Series 2400, the diagnostics include identity, the Standard
Event Status Register Power-On bit, output state, and the oldest system error.
A failed role is quarantined after the incident: the run does not resume, and a
new run is blocked until the SMUs are disconnected and reconnected. The
software never turns an output back on as part of diagnosis or recovery.
