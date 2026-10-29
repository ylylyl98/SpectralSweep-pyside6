# Installation, drivers and first-run configuration

**English** | [简体中文](installation.zh-CN.md) | [Project home](../README.md)

Install the components for the devices you actually use. A disconnected UI
preview needs no instrument software. An EXE build includes its Python runtime
and Python packages; Windows device drivers and vendor applications are separate.

## Choose an installation path

| Component | Portable EXE | Source / development |
| --- | --- | --- |
| SpectralSweep | Extract the complete release ZIP | Obtain the repository |
| Python, PySide6, NumPy and application Python packages | Included | Install Python 3.13 x64, then `requirements-quick.txt` |
| Git | Not required | Recommended for cloning and updates; a source archive also works |
| PyInstaller / pytest | Not required | Only needed to build EXEs / run tests, via `requirements-ci.txt` |
| Instrument drivers, vendor software and required .NET Framework | Install for the selected devices | Same requirements as the EXE |
| Private ST133 migration runtime and tools | Not included; migration currently uses source + Python | Obtain the separate runtime ZIP and follow the dedicated guide |

Downloads may require a vendor account, an instrument serial number or an
existing software entitlement. Use the package matching the instrument and
Windows version; the newest SDK is not automatically compatible with older hardware.

## Portable EXE

1. Open [GitHub Releases](https://github.com/ylylyl98/SpectralSweep-pyside6/releases).
   `main` builds are **pre-releases**; a formal release is published from a `v*` tag.
2. Download the `SpectralSweep-<version>-windows-x64.zip` asset and its `.sha256` file.
3. Optionally compare the downloaded file's SHA256 with the companion file:

   ```powershell
   Get-FileHash .\SpectralSweep-*-windows-x64.zip -Algorithm SHA256
   Get-Content .\SpectralSweep-*-windows-x64.zip.sha256
   ```

4. Extract the entire ZIP to a local folder. Run `SpectralSweep.exe` and retain
   `SpectralSweep-worker.exe` and `_internal` in the same directory.
5. Complete the device-specific setup below before connecting equipment.

From the extracted directory, this opens the isolated nine-workspace preview:

```powershell
.\SpectralSweep.exe --preview-workspaces
```

For an offline UI/worker audit that writes a report and screenshots:

```powershell
.\SpectralSweep.exe --smoke-test "$env:TEMP\SpectralSweep-smoke"
```

Neither command connects instruments. A successful audit does not verify hardware drivers or acquisition.

<a id="source"></a>
## Source setup

Install [Python 3.13 x64](https://www.python.org/downloads/windows/).
Choose the 3.13 series used by this project's CI; the Python site may offer a
newer default series. Install [Git for Windows](https://git-scm.com/install/windows)
if you want to clone and update with Git, then open a new PowerShell window:

```powershell
git clone https://github.com/ylylyl98/SpectralSweep-pyside6.git
Set-Location SpectralSweep-pyside6
py -3.13 -m venv .venv-pyside6-313
.\.venv-pyside6-313\Scripts\python.exe -m pip install -r requirements-quick.txt
.\.venv-pyside6-313\Scripts\python.exe main.py --preview-workspaces
```

The environment name matches `launch.bat`'s discovery rules. Explicit interpreter
paths avoid needing a PowerShell activation script. `requirements-quick.txt`
includes the application dependencies and pins the validated PySide6 6.11.0 runtime.

Daily source launch: double-click `launch.bat`, or run `.\launch.bat` in PowerShell.
It creates/selects the managed environment and checks `requirements.txt` when
dependencies change. `.\launch.bat --install` only checks/installs Python dependencies;
`.\launch.bat --update-deps` upgrades them. Reapply `requirements-quick.txt` if you
want to return an upgraded environment to the pinned Qt version.

`python main.py --mock` simulates the spectrometer but retains the normal application
runtime. Use `--preview-workspaces` for the fully isolated, disconnected preview.
Developer test/build commands are in the [README](../README.md#development-and-validation).

## Instrument prerequisites and official downloads

The sections below apply to both EXE and source users unless stated otherwise.
The corresponding Windows device should be identifiable before selecting it in SpectralSweep.

<a id="lightfield"></a>
### Princeton Instruments / LightField

- Obtain [LightField](https://www.teledynevisionsolutions.com/en-ca/products/lightfield/?model=LF&segment=tvs&vertical=tvs-princeton-instruments)
  with the Automation components and drivers matching your camera and spectrograph.
- Enable/install the [.NET Framework](https://dotnet.microsoft.com/en-us/download/dotnet-framework)
  version required by that LightField installer if it is not already present.
  Installing Python's `pythonnet` package does not install the vendor application or this Windows runtime.
- Check `LIGHTFIELD_ROOT` in Windows environment variables. It must point to the
  actual LightField installation root used on this PC; the code also searches
  its `AddInViews` directory and loads the LightField V5-named Automation assemblies.
  If the installer did not set the variable, add it for your Windows user and
  restart SpectralSweep and any open launching terminal.
- First establish a working camera/spectrograph experiment in LightField, then
  select the intended setup and output route in SpectralSweep.

The standalone PICam SDK/driver does not supply the LightField Automation
interface used by this backend. DLL files copied from another PC are not a complete installation.

<a id="andor"></a>
### Andor SDK2 cameras and Shamrock

Use the [Andor SDK and driver portal](https://andor.oxinst.com/products/software-development-kit/software-development-kit)
to obtain the driver and SDK2/Shamrock components for your actual camera and spectrograph.
This application uses SDK2 for the supported CCD/InGaAs backend; an SDK3-only installation is not equivalent.

Selected DLLs are already present in `andor dll/` and in the release's
`_internal/andor dll/`. Those files do not install the USB/PCI camera or spectrograph drivers.
In **Settings**, check the SDK2 and Shamrock DLL directories. Bind camera serials
and models to the Si/InGaAs roles rather than assuming another PC's enumeration order.
Validate vendor discovery, then close competing applications before connecting.

<a id="visa"></a>
### VISA instruments, Keithley SMUs and GPIB

Install [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)
or a compatible system VISA implementation, plus your USB/serial/GPIB adapter's
driver. NI GPIB controllers may also require
[NI-488.2](https://www.ni.com/en/support/downloads/drivers/download.ni-488-2.html).

The current controllers use PyVISA's default system resource manager. The
bundled/installed `pyvisa` and `pyvisa-py` Python packages do not install NI-VISA
or a GPIB kernel driver. Use NI MAX or the equivalent vendor utility to check
discovery, then select this PC's actual VISA resource and channel role in the app.
Check the instrument's intended output and compliance settings before a run.

<a id="pm100d"></a>
### Thorlabs PM100D

Obtain drivers/tools from [Thorlabs Power Meter Software](https://www.thorlabs.com/software-pages/PM100x)
and provide the [VISA runtime](#visa). The current GUI adapter communicates by
**PyVISA/SCPI**; the repository's legacy `TLPMX.py` and `TLPMX_64.dll` are a separate interface.

OPM can assign the newer TLPMX device driver. If the meter works in OPM but
does not appear as a VISA USB instrument, consult the
[official OPM manual's Driver Switcher section](https://media.thorlabs.com/contentassets/012d23129bc54ff2bfdbf4c6d4a306fa/tl_opm_v7.0.pdf?v=1117095710)
and select the **NI-VISA/PM100D** driver path used by this GUI. Driver selection
also affects other applications using that meter. Close them while changing it,
then verify the meter's USB resource and serial in the VISA utility and SpectralSweep.

<a id="mff101"></a>
### Thorlabs MFF101 flipper

Install the device-compatible [FTDI D2XX driver](https://ftdichip.com/drivers/d2xx-drivers/)
or the matching Thorlabs driver package. The backend uses pylablib's direct FTDI
interface; Kinesis application software and its .NET SDK are not required by this path.

Scan/select the eight-digit USB serial and confirm the physical position mapping.
MFF101 is optional: automatic routing is **off by default**. Without a flipper,
keep **Settings > Instruments > MFF101 light paths > Use MFF101 automatic routing on this PC** unchecked.
If reusing an old profile, check this setting and save the configuration.

<a id="motion"></a>
### Elliptec and Newport motion

- **Elliptec:** provide the USB adapter's virtual-COM driver, commonly
  [FTDI VCP](https://ftdichip.com/drivers/vcp-drivers/). The adapter uses a `COMx`
  serial connection, unlike MFF101's direct D2XX connection.
- **Newport ESP300/ESP301:** provide [VISA](#visa) and the driver for the actual
  USB, RS-232 adapter or GPIB interface. Native ESP301 USB requires the matching
  driver from [Newport](https://www.newport.com/p/ESP301-3G); see its
  [USB driver installation instructions](https://www.newport.com/medias/sys_master/npresources/hb5/h6a/9954466168862/ESP301%20-%20USB%20Driver%20Installation%20Manual/ESP301-USB-Driver-Installation-Manual.pdf).

Select the controller model, address, axis and matching baud rate. The app's
ESP301 native-USB default is 921600; its RS-232 default is 19200, subject to the
actual controller configuration. Newport's .NET SDK is not used by this ASCII/VISA backend.
Connecting a Newport axis enables its motor; read the [motion guide](newport-rotation.md) first.

<a id="magnet"></a>
### attoDRY1000 / APS100 and attoDRY2100

**attoDRY1000 / APS100:** obtain the matching virtual-COM driver from
[attocube support](https://www.attocube.com/en/ressources/services-and-support)
and provide [VISA](#visa). This backend talks directly to APS100 and does not use
the attoDRY2100 SDK. Select the actual address and the magnet's approved configuration.

**attoDRY2100:** the repository includes a Python SDK copy in `CRYO2100/`, also
packaged as `_internal/CRYO2100/` in the EXE bundle. Confirm compatibility with the
instrument's firmware; request the matching SDK from
[attocube support](https://www.attocube.com/en/ressources/services-and-support) if needed.
Set `attodry2100.sdk_directory` to the actual SDK directory on this PC, and set
`host` and `channel` for your system. The loader expects either `__init__.py`
exporting `Device`, or an `atto_device/__init__.py` package within that directory.
The saved developer drive path is an example, not a location available on a new computer.
EXE users do not need to install a separate Python interpreter to load a compatible SDK copy.

The SDK directory is an advanced JSON setting. Close SpectralSweep before editing
`%APPDATA%\SpectralSweep\config.json`; merge the appropriate fields into its existing
`attodry2100` object and retain the other settings. For example:

```json
{
  "attodry2100": {
    "sdk_directory": "C:\\SpectralSweep\\_internal\\CRYO2100",
    "host": "192.0.2.10",
    "channel": 0
  }
}
```

This is a fragment, not a replacement configuration. Replace the example path,
documentation-only IP address and channel with this PC's folder and instrument settings.

<a id="daq"></a>
### NI DAQ analog input/output

Install [NI-DAQmx](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html)
**only if the setup uses NI DAQ hardware**. Confirm the device and physical
channel names in NI MAX. The Python `nidaqmx` package included with the app is
not the NI-DAQmx system driver. A VISA-only Keithley setup does not require
DAQmx merely because the app includes that Python package.

<a id="esp32"></a>
### ESP32 imaging stage

The controller requires this repository's `stage-v2` firmware from
[`firmware/esp32_stage`](../firmware/esp32_stage). Provide the USB connection
driver appropriate for the actual board, following
[Espressif's ESP32-S3 connection guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/get-started/establish-serial-connection.html).
Boards may use a USB-UART bridge or native USB; a single driver brand does not cover all boards.

[PlatformIO](https://docs.platformio.org/en/latest/core/installation/index.html)
is needed when building/flashing firmware, not for normal EXE operation with an
already configured board. Select this PC's upload/COM port rather than the
example port in the firmware project. Confirm the firmware handshake and use
the [stage setup procedure](esp32-imaging-stage-trial.md) for reference and travel configuration.

<a id="winspec"></a>
### WinSpec / ST133 InGaAs

There are two distinct acquisition paths:

| Path | What must already be provided |
| --- | --- |
| Legacy remote WinSpec / experimental PVCAM | A configured remote camera PC/VM with the matching vendor drivers/runtime and Python 2.7 camera service. The WinSpec path also needs licensed WinSpec and COM; the experimental PVCAM service calls vendor DLLs directly. Set the reachable host/port. See [side-port setup](winspec-side-port.md) and [PVCAM backend](pvcam-backend.md). |
| Local Windows 11 WinUSB | The privately supplied `ST133-InGaAs-Win11-x64-v6.zip`, exact device/interface registration and local binding. Windows supplies WinUSB; the vendor/helper runtime is not in the public EXE. Follow [the new-PC procedure (中文)](st133-new-pc-setup.md) and [Microsoft's WinUSB guidance](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/winusb-installation). |

The documented local installation and validation workflow currently needs
**source + Python 3.13 x64**. Ordinary EXE installation does not require Python.
The source procedure does not deploy or validate the EXE's separate runtime/state
directory; its completion is not proof of a working EXE WinUSB deployment.
A local standalone detector test does not need XP,
WinSpec GUI or LightField; the **integrated SpectralSweep InGaAs workflow still
uses LightField to control the spectrograph/output route**.

Local WinUSB support is experimental and restricted to the supported hardware,
manual 1D capture and the documented exposure/accumulation range. The procedure
is not a generic driver replacement for other cameras. Obtain the private
runtime through the lab's existing transfer process; do not expect it in public Releases.

<a id="first-run"></a>
## First-run configuration

1. **Identify equipment.** Check Windows Device Manager and vendor discovery.
   Confirm model/serial and release competing device handles before connecting in the app.
2. **Configure this PC.** Check detector backend and optical profile, SDK paths,
   camera serial bindings, COM/VISA addresses, axes, baud rates and magnet host/channel as applicable.
3. **Set storage and sample.** Choose a writable output folder and sample ID.
4. **Check optional routing.** Keep MFF101 automatic routing disabled if no
   flipper is installed; otherwise verify its mapping against the physical optics.
5. **Save and verify.** Save the local configuration, then check a small
   acquisition and the saved CSV/metadata before starting a larger workflow.

Settings are stored at `%APPDATA%\SpectralSweep\config.json`. Source checkouts
may import the repository's `config.json` as a compatibility default. Review
machine-specific values rather than copying another PC's whole configuration.
The UI does not automatically reconnect instruments or resume a measurement on startup.

Notifications are optional. A local ntfy subscription can be configured in the
app without installing a driver; see [notification setup](usage-reference.md#per-pc-ntfy-subscriptions).
The older text quick-start is [here](../00_启动说明.txt).

## Troubleshooting

| Symptom | Check first |
| --- | --- |
| Missing DLL/module when starting the EXE | Extract the complete ZIP and keep the worker/`_internal` files with the main EXE. |
| LightField cannot load | Vendor installation, Automation assemblies, required .NET Framework and `LIGHTFIELD_ROOT`; restart the launching process after changing environment variables. |
| PM100D works in OPM but is absent in VISA | Driver Switcher assignment and VISA installation; see [PM100D](#pm100d). |
| No USB/COM/VISA device | Matching device/interface driver, Device Manager, actual serial/address and vendor discovery utility. Python package installation alone is insufficient. |
| attoDRY2100 SDK directory not found | Replace the old computer's SDK path with the local compatible `CRYO2100`/vendor SDK directory. |
| Source launch has a Qt version error | Run the intended environment's Python and install `requirements-quick.txt`. |

For an [issue report](https://github.com/ylylyl98/SpectralSweep-pyside6/issues),
include the build's `version.json`, Windows version, device/backend, connection
method, reproduction steps and error text. UI/mock/CI success is not a hardware commissioning result.
