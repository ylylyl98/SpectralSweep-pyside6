# SpectralSweep

SpectralSweep is a PySide6 desktop application for spectra acquisition and sweep-driven optical measurement workflows. The repository is organized around the current desktop UI and its instrument-control runtime.

The application is intended for lab setups that combine Princeton Instruments LightField spectroscopy with voltage control, motion control, and optional optical power measurements. The desktop UI keeps instrument connections, sweep setup, live preview, and data capture in one operator-facing workflow.

## Main Features

- PySide6 desktop interface with a docked instrument-control panel and dedicated workflow tabs
- Live spectrum viewing for the LF6 / LightField spectrometer path
- Presets-based spectra sweep planning with loop tables, batch conditions, and CSV export
- MegaSweep voltage-mapping workflow for Vtg/Vbg and D/Vbias acquisition patterns
- BFP viewing and export tools
- Hardware-controller wrappers for LF6, Keithley SMU workflows, rotation stages, linear stage, and Thorlabs PM100D
- Mock LF6 mode for UI development without live spectrometer hardware

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
   Princeton Instruments LightField for LF6 automation, VISA support for Keithley / Newport communication, and Thorlabs PM100D driver files where applicable.

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
  LF6 / LightField connection and acquisition control.
- `controllers/smu_controller.py`
  Keithley / IV workflow integration used by sweep panels.

## Hardware Notes

Some modules depend on lab-specific hardware and vendor runtimes:

- `lf6_automation.py` integrates with Princeton Instruments LightField through `pythonnet`.
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
