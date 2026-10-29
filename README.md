# SpectralSweep

Windows EXE 下载见本仓库 **Releases**。main 更新后自动运行完整测试、构建和隔离启动检查，
通过后发布预发布版；`v*` 标签生成正式版。使用与维护说明见
[`Windows 自动发布`](docs/windows-release.md)。

## 日常启动（Windows）

**双击本项目根目录的 [`launch.bat`](launch.bat)，即可打开 SpectralSweep。**
它与本 README、`main.py` 在同一层。根目录只保留这一个主程序 BAT 入口；
请勿使用 `tmp/` 旧副本中的同名脚本。首次启动会检查并安装依赖，可能需要联网等待。

中文速查见 [`00_启动说明.txt`](00_启动说明.txt)。仪器手册已归入 `docs/manuals/`，
截图在 `docs/screenshots/`，旧 Notebook 和参考脚本在
[`docs/reference/`](docs/reference/README.md)。[`tools/`](tools/README.md) 下的
脚本用于开发、维护或仪器诊断；其中 `winspec/*.cmd` 不是桌面主程序入口。

SpectralSweep is a PySide6 desktop application for spectra acquisition and sweep-driven optical measurement workflows. The repository is organized around the current desktop UI and its instrument-control runtime.

## Qt Quick 界面（默认）

`launch.bat` 和 `main.py` 默认打开 Qt Quick 前端：9 个工作区、仪器参数、
表单与表格、科学图表、参考光谱、历史及业务弹窗采用统一的深浅主题。
双栅序列优先展示，Apply plan、运行/停止固定在右侧预览栏底部；Dual Gate 不再保留全宽底部操作条。Batch Sweep Rows 保持在中间参数区，每条记录单行展示全部字段，无横向滚动或字段分页。其他宽表格按记录自动换行。
Qt Widgets 仍提供窗口容器和隐藏的兼容状态；设备执行逻辑及实验数据格式继续复用。

```powershell
# 全部页面隔离预览：临时配置、设备断开、测量按钮禁用，F5 重载
python main.py --preview-workspaces

# 合成光谱、历史和序列演示
python main.py --preview-quick

# 默认 Quick 前端，连接同一实验后端
python main.py

# 原界面
python main.py --ui widgets
```

Qt Quick 入口要求 PySide6 6.9 或更高版本；本轮验证版本为 6.11.0，
可用 `python -m pip install -r requirements-quick.txt` 安装对应 Qt 版本。
`launch.bat` 会转发相同参数；旧环境会按照更新的 `requirements.txt` 检查依赖。
实现范围、基准和剩余迁移门槛见 [Qt Quick 迁移记录](docs/qt-quick-migration.md)。
隔离预览的 **Dual Gate sequence** 标签提供交错扫描和 When 过滤两个例子；
正式 `--ui quick` 的 Dual Gate 页面也使用相同预览。支持分页、跟随当前步、
完整窗口逐步跳转及相邻步骤差异，详见 [序列预览设计与验证](docs/dual-gate-sequence-review.md)。
布局检查与实施结果见 [布局审查](docs/qt-quick-layout-review.md)。模拟回放和界面验证不代表真机长时间采集已经验证。
全页迁移补缺、启动/切页/输入优化与模拟连续谱结果见 [响应速度检查](docs/qt-quick-performance-20261026.md)。

The application is intended for lab setups that combine either Princeton Instruments LightField or an Andor SDK2 camera with a Shamrock spectrograph, voltage control, motion control, and optional optical power measurements. The desktop UI keeps instrument connections, sweep setup, live preview, and data capture in one operator-facing workflow.

## Main Features

For the tested ST133/InGaAs on Windows 11 x64, see the
[new-PC installation guide (中文)](docs/st133-new-pc-setup.md).
The source and installer are public; the pinned vendor runtime travels in a
separate private ZIP. This backend supports experimental manual acquisition.

- PySide6 desktop interface with a docked instrument-control panel and dedicated workflow tabs
- Live spectrum viewing with selectable LightField, Andor Si CCD, or Andor InGaAs CCD backends
- Presets-based spectra sweep planning with loop tables, batch conditions, and CSV export
- MegaSweep voltage-mapping workflow for Vtg/Vbg and D/Vbias acquisition patterns
- BFP viewing and export tools
- Hardware-controller wrappers for LightField/Andor, Keithley SMU workflows, rotation stages, linear stage, and Thorlabs PM100D
- Thorlabs MFF101 manual controls and automatic setup routing with verified position readback
- Mock spectrum mode for UI development without live spectrometer hardware

## Sample settings

Selecting a Sample ID with saved settings restores that sample's setup. A new
Sample ID, or one without a saved setup, keeps the current settings, active tab,
and unapplied drafts, then saves an independent profile for the new sample.
At startup, a sample without its own profile uses the last saved session.
Measurement records in run history remain associated with their original sample.

## Dual Gate numeric inputs

Sweep Line Calculator constants and numeric Loop **Values** use the same syntax:

- `4`: one value.
- `-4,0,4` or `[-4,0,4]`: an explicit list; order and duplicates are kept.
- `linspace(-4,4,5)` or `(-4,4,5)`: five evenly spaced points including both
  endpoints, giving `[-4,-2,0,2,4]`. The third argument is a point count (an
  integer of at least two).
- `-4:2:4`: a `start:step:stop` range. It includes the stop only when the step
  lands on it. Lists and sequences can be mixed, such as `-4:2:0,3,5`.

Constants remain limited to 500 values within -200 to 200 V. Loop rows allow
up to 100,000 values in the selected parameter's units. Wavelength and exposure
must be positive, accumulations must be positive integers, and stage positions
must fit the active stage's travel range. Measurement setup continues to use
setup names. Rows in the same group must resolve to the same number of points.

Old sample profiles retain their original resolved Loop values and **This run**
selections. Before this change, only Stage Position expanded `linspace` or its
parenthesized shorthand; other numeric rows treated the arguments as individual
values. Those old expressions are restored as explicit lists so their scan
meaning is preserved. Newly entered expressions use the shared syntax above.

## MFF101 flipper

The MFF101 is optional. Automatic routing is **off by default** on a new PC,
so setup selection, spectrometer connection, and acquisition work without a
flipper or its driver. On a PC with a flipper, enable **Settings > Instruments > MFF101 light
paths > Use MFF101 automatic routing on this PC** and save the configuration.
Existing saved routing choices are preserved; on a PC without a flipper, clear
that checkbox and click **Save config to disk** if routing was previously enabled.
This choice is stored in the local per-user configuration and is not restored
from sample or measurement history. Manual flipper controls remain available
when automatic routing is off.

Expand **MFF101 Flipper** in the Instruments sidebar. Connect the powered
flipper by USB, click **Scan** (or enter its eight-digit serial number), then
**Connect**. **Position 1** and **Position 2** move to the two device positions;
the display reports arrival only after reading back the target. Under
**Settings > Instruments > MFF101 light paths**, the default mapping is **Position 1 → Andor**
and **Position 2 → LightField**. Edit either name when the physical paths change,
then click **Save config to disk**. The sidebar immediately shows the configured
paths and the name corresponding to the current position. Names are global
equipment settings and are not replaced when switching samples or loading old
measurement settings. Editing a name does not move the flipper.
Position is also refreshed once per second, including changes made with the
flipper's physical button. **Read position** requests an immediate refresh.

The existing `pylablib` dependency uses the FTDI D2XX USB driver. Install the
Thorlabs/FTDI driver if discovery fails, and close other applications using the
same flipper before connecting. Connection, polling, and movement run on a
background thread. A failed read clears the position and disconnects; a failed
move reports an error instead of assuming arrival. Disconnecting closes the USB
connection; it does not stop an already-issued physical flip.

Manual moves and automatic setup routing allow at most **two motion commands**
(the initial command plus one retry). The first command has a 5-second arrival
window; the whole operation has a 10-second deadline. Before retrying, the app
reads the position: a target readback completes the operation, an in-transit
readback only continues waiting, and only the other known endpoint permits a
retry. USB/read/write errors fail immediately without reissuing a command.
Measurement Stop and application shutdown end polling after the current USB
call and prevent further commands; they do not halt a physical flip. A second
failure or unresolved motion blocks acquisition and reports an unverified
position. Retry attempts are recorded in the application log.
Spectrum **Stop** and **Abort** also cancel queued acquisition requests and
pending flipper checks, so a delayed position read cannot start a new exposure
after cancellation. A subsequent acquisition uses a fresh cancellation state.

Some MFF101 APT firmware reports the internal model `MFF002` and a zero firmware
serial. This variant is accepted only with the filter-flipper hardware type and
one channel; the selected USB serial still identifies the connection. A nonzero
firmware serial must match. See the [MFF101 driver example](https://github.com/calico/triplet_tumbling_microscopy/blob/main/lib/thorlabs_MFF101.py)
for the internal model identifier. pylablib may print a model-prefix warning for
this firmware even though the connection is supported.

The app remembers the serial number. Starting the app, loading settings, and
changing samples do not connect or move the flipper automatically. With automatic
routing enabled, explicitly selecting a measurement setup routes **Andor Si / InGaAs → position 1** and
**LightField / WinSpec → position 2**. Settings includes an automatic-routing
checkbox and editable setup-to-position dropdowns, separate from descriptive
path names. In **Dual Gate Measurement setup** recipes and **2D Sweep optical
conditions**, execution automatically routes each selected setup before its
measurements, including setups nested inside gate points. Initial recipe
preflight also visits the requested setups. Editing a draft does not move the
flipper. Connecting/switching setups and acquisition preflight also verify
the configured position. Routing failures block that operation. A disconnected
flipper connects using its saved serial, or the single discovered USB device;
ambiguous discovery requires selecting a serial. Simulation does not route real
hardware, and manual flipper actions are locked during measurements.
The Spectrum sidebar metadata
snapshot includes its last observed position and readback timestamp.

## LightField experiment metadata

LightField runs in Power Sweep, MegaSweep, BFP, and both MCD tabs
automatically read instrument settings immediately before each capture. The
experiment's `*.experiment.metadata.json` stores distinct configurations under
`settings.snapshots`, separate from requested UI settings. The compatibility
view under `observed.lightfield.settings_snapshots` links to those snapshots.
Readbacks include exposure, exposures per frame, combination method, stored
frames, grating label, center wavelength, ports/slits, readout/gain, detector
temperature setpoint, ROI/binning, correction flags, device identity, experiment
name, application assembly version, and detector wavelength calibration.
Unsupported or failed reads are `null` with a reason in `unavailable`.

The adjacent `*.experiment.events.jsonl` contains capture start/completion
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
authoritative saved axis. Optional readback failures are logged without stopping
acquisition. A failure to store settings or capture events marks metadata as
degraded and prevents the run from reporting successful completion, while
allowing detector cleanup. Compatibility settings references are unique within
the run, including when switching setups or recreating a recorder. Recovery
counts capture attempts consistently with live recording, without counting
completion events again. This metadata is provenance and is not automatically
restored to hardware when loading saved settings.

Dual Gate (Presets) uses file-boundary metadata for LightField, Andor, and
WinSpec. Before a file's first acquisition it synchronously saves the actual
settings and full-precision calibration, then appends `file_started`. A
spectrometer configuration or backend change triggers a fresh snapshot and a
`file_settings_changed` event for an already open file. Unchanged points skip
metadata snapshot queries and capture events; hardware safety validation and
CSV writes still occur on each point. After CSV close succeeds, `file_completed`
records the file ID, settings ID, start/end time, successful point count and CSV
row count. Errors/stops write `run_interrupted` before cleanup and a
`file_failed`/`file_stopped` summary. These journal records are synchronously
fsynced. Only scalar counters for unfinished files remain in memory, with no
pending-write queue or per-point timing/temperature history. The final manifest
registers outputs together and reports `summary.completed_points`; capture
attempt counts are intentionally not recorded in this mode. Keep the event
journal with the manifest to retain file summaries after an interruption.

Dual Gate startup validates and saves the applied plan on its run worker before
instrument preparation. Run immediately shows a preparation status; the log
reports validation and save durations, and Stop remains available. A cancelled
or failed startup sends no instrument commands. Plan and filename inputs remain
those captured when Run was clicked, even if the next draft is edited.

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

The launcher installs dependencies on first use and when `requirements.txt`
changes; successful installs are cached for the selected Python environment.
Ordinary launches then skip `pip`. Use `launch.bat --install` to install/check
dependencies without opening the app, or `launch.bat --update-deps` to upgrade
dependencies before launching. Other arguments are forwarded to `main.py`.

To force mock LF6 mode:

```bash
python main.py --mock
```

The instrument sidebar keeps the current Sample ID and save status above the
device controls. Use **Hide**, **View > Show sidebar**, or `Ctrl+Alt+B` to toggle
it. Manual Keithley controls keep the step size, per-channel target/Ramp,
step/read/zero buttons, and Read All/All to zero actions visible when expanded.

## Faster UI iteration

After restarting once to load this feature, use **View > Reload styles**
(`Ctrl+Alt+R`) to reapply `ui/styles/application.qss` to the running app.
This retains existing widgets, field values, plots and instrument controllers.
Common button, status, table and text appearances are defined in this QSS;
Python widgets select semantic roles through `ui/style_roles.py`. Reference
swatches retain their individual data colors. Edit QSS for visual changes rather
than adding panel-local style sheets.
For automatic stylesheet refresh during development, start with:

```powershell
python main.py --watch-styles
```

Use **View > Open Spectrum preview…**, or launch the preview directly:

```powershell
launch.bat --preview-spectrum
```

The preview displays the real Spectrum panel with synthetic spectra and a
simulated controller. It uses a separate process, temporary configuration,
history index and default output folder; it does not start instrument
controllers, notifications or the experiment watchdog. **F5 / Reload preview**
restarts only the preview process to pick up Python/layout changes, resetting
the displayed scene. QSS edits refresh automatically without resetting it.
The simulated connection toggle, sample error action, 1D/2D acquisition and
continuous acquisition can be used to inspect different UI states.

Preview scratch files are deleted when the preview closes. Use an explicitly
chosen export location if you want to keep synthetic output. The preview is a
UI development aid, not validation of hardware timing or driver behavior.

**View > Refresh current interface** (`Ctrl+Alt+U`) replaces a supported page while
retaining its instrument controllers and connections. The View menu also has
explicit Spectrum, Dual Gate and Settings reload actions.

| Page | Python modules loaded | State retained |
| --- | --- | --- |
| Spectrum | `spectrum_panel`, `spectrum_plot`, `andor_controls_widget` | Acquired 1D/2D data, unsaved reference spectra, display ranges, settings and the existing Calibration editor |
| Dual Gate | `presets_panel`, `preview_widget`, `loop_run_selection`, `setup_sequence_editor` | Separate drafts/applied plans, run selections, execution order, undo history, logs, completed-run progress and the spectrum viewer |
| Settings | `settings_panel` | Editable values, notification-name draft, selected category and status messages |

In Qt Quick mode, refresh also loads the registered Python layout helpers:
the Dual Gate sweep calculator and gate sections, Spectrum acquisition sections
and reference list, and Settings sections. The workspace layout, Quick table
editor and parameter-form Python adapters are rebuilt with the page. Valid
pending edits, table selections, disclosure choices, splitter sizes and scroll
positions are preserved; an invalid edit must be corrected before refreshing.
New Quick surfaces are checked before the old interface is retired. A layout
that cannot be built leaves the existing page available.

These modules are under `ui/`. Acquisition, instrument operations, calibration
analysis and saving must finish first; reload never aborts them. Syntax,
construction, state-transfer or binding failures retain the old page. A Dual
Gate change that would reinterpret the applied plan is rejected rather than
silently changing the next measurement. Reload is explicit; source watching
only reports pending changes and never automatically replaces Python code.

The status bar's **Code** button and **View > Code update status…** show files
changed since startup, the page/style reload that can apply them, and files that
still require restart. A successful reload clears only the code applied to that
page. Shared Spectrum plot/control modules may still have old instances on
other pages, so their changes remain marked for a full restart. Shared Quick
helpers are tracked per supported page; refreshing one page does not confirm
that every page has been updated. The Code button shows both refresh availability
and a pending restart when applicable. Other pages, QML/JS components, the global
window shell, controllers, drivers and the reload infrastructure require a
safe restart. Constructors and restorers must remain free of instrument commands
and event-loop pumping. A running older version needs one initial safe restart
to load these development actions.

Calibration is unfinished and hidden from the main navigation by default.
**View > Show experimental Calibration page** reveals it for development;
saved calibration records and their use by Spectrum remain available. Restoring
a session/sample does not reveal the hidden page.

Missing/empty or structurally incomplete QSS leaves the previous
style in place; Qt still validates supported properties and selectors. The
existing light appearance is retained; this feature does not add a dark theme.
When packaging, include `ui/styles/application.qss` at the same relative path.

## Per-PC ntfy subscriptions

Open **Settings > Notifications > PC Notifications**, enter a descriptive name such as
`Attodry-PC`, and click **Save and lock PC name**. Click **Copy URL** and subscribe
to that URL in ntfy. Saving activates notifications immediately; no restart is
needed. The name becomes read-only after saving and remains visible with the
subscription URL on later launches. Each setup creates a unique
short topic such as `ss-attodry-pc-k7m4qx`
(URL: `https://ntfy.sh/ss-attodry-pc-k7m4qx`). The topic includes up to 16 characters
of the PC name and a six-character random suffix; Settings keeps the full name.
Existing saved URLs
are preserved so current subscriptions keep working. For an existing long URL,
click **Use shorter URL (restart required)** in PC Notifications, restart the app,
and subscribe to the new URL. The PC name stays locked, and the old URL is retained
in the local configuration as `previous_url`. Each PC gets a separate
topic, even if two PCs are given the same name. Completion, warning, error, crash,
and watchdog alerts all use that PC's topic. The previous shared topic no longer
receives alerts from updated installations.

The Settings panel always shows the saved subscription. Optional command-line
setup and display are also available:

```powershell
python -m app.notification_config --name "Attodry-PC"
python -m app.notification_config
```

The configuration is saved outside Git at
`%PROGRAMDATA%\SpectralSweep\notifications.json` on Windows and shared by the
PC's Windows accounts. App updates and computer renaming do not change it.
There is no rename control in the app, and rerunning `--name` refuses to overwrite
the setup. This prevents accidental changes; it is not an administrator security
lock. Back up this file to preserve the subscription after reinstalling Windows.
Do not copy it to other PCs, since that would share the same subscription.

Until setup is saved, notifications remain off and no topic is created
automatically. To deliberately reset the setup,
close the app, remove the configuration file, rerun setup, and subscribe to the
new URL. On systems without `PROGRAMDATA`, the file is stored under
`~/.config/SpectralSweep/notifications.json` for the current user.
If the file is unreadable or damaged, the app emits a runtime warning and runs
with notifications disabled. Restore the saved file to recover the same topic.

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

The **Spectrum → Spectrograph** center and the shared **Settings** center accept
**0 nm** for zero-order imaging / alignment. Photon energy is shown as
**N/A (zero order)** until a positive wavelength is selected. Andor moves with
the dedicated Shamrock zero-order command and checks the hardware confirmation.
Zero-order Spectrum captures display and export detector pixel coordinates;
stored wavelength coefficients are not used for that position. WinSpec retains
its full pixel frame without applying or cropping to a saved wavelength fit.
Selecting a positive center restores photon energy and the usual backend axis
(wavelengths for PIXIS / Andor, pixels or matching calibration for WinSpec). This
control does not extend wavelength-calibration coverage for automated sweeps.

### MCD 2100 magnet preparation

`Start MCD 2100` performs a read-only magnet safety preflight, requests Driven
mode when required, and verifies three consecutive safe readbacks before it
positions the field at Start. The `Prepare magnet` action runs that magnet-only
operation independently; it does not require LightField, an SMU, a sample ID,
or an output directory, and leaves the controller connected at the verified
Start field.

The attoDRY2100 configuration keys `mode_prepare_timeout_s` (default 300 s)
and `mode_lead_tolerance_t` (default 0.001 T) bound the operation. Readiness is
an observed software condition: Driven=true, Persistent=false, heater on,
finite lead/current telemetry within tolerance, and HState=`IDLE`; it is not a
vendor guarantee that a physical transition has completed. Firmware states that
are unknown or missing remain unready. `getLeadsHot` reports current presence in
the leads and is informational, not an overtemperature alarm.

Cancellation cooperatively stops new requests while an in-flight SDK call is
allowed to drain. If a mode request may have reached the instrument, the panel
keeps device mutations and shutdown blocked until a strict `Prepare magnet`
retry confirms recovery. No physical commissioning or hardware validation is
performed by the application tests.

The MCD 2100 sidebar provides a read-only **Refresh ramp tables** action and a
local **View ramp tables** action. After a complete display telemetry cycle,
one automatic read is arranged for the current connection generation; it does
not repeat until reconnect. The automatic read opens no dialog. Refresh reads
current and default raw ramp rows through the existing attoDRY2100 connection,
while View never communicates with the device. Counts are bounded to 32 rows and
requested indices are shown as reported; the application does not infer units,
index bases, or preset meanings. Count and row failures remain visible beside
successful rows. A client timeout keeps shared controls locked until the owner
thread drains and then displays any partial report; the diagnostic read does not
issue field, mode, Stop, reconnect, or close commands. Ramp tables are not read
automatically more than once per connection, or during preparation/acquisition.
Disconnects retain the previous report as historical/stale data; a failed new
read does not make that old report current.

### MCD 2100 display telemetry

The connection panel shares display-only magnet and temperature reads between
the manual refresh, background display polling, and applied-temperature monitor.
Repeated requests join the current owner operation; a successful result may be
reused for 0.5 seconds in the same connection generation. The background cycle
reads magnet then temperature and schedules its next cycle one second after
both requests drain. Safety preflight and acquisition verification continue to
use fresh reads and never consume this cache.

The panel shows magnet and temperature values as soon as each group arrives,
with separate last-success ages. A client timeout keeps the display request
reserved until the owner drains; partial data and errors remain visible. The
controller exposes bounded queue/request diagnostics and the adapter records
actual SDK RPC durations for troubleshooting. These timings identify queue and
device bottlenecks; they are not a real-device speed benchmark.

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
|-- 00_启动说明.txt
|-- launch.bat                 # Daily Windows launcher
|-- docs/
|   |-- manuals/
|   |-- screenshots/
|   `-- reference/             # Historical notebooks and scripts
|-- tools/                     # Maintenance and diagnostics
|-- app/
|   |-- devices/
|   `-- engine/
|-- controllers/
|-- ui/
|-- utils/
|-- iv_automation.py
|-- lf6_automation.py
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

## Motion Sweep with ND calibration

In the Motion Sweep tab, open the ND calibration controls, enter stage
positions, and use **Scan and save calibration** with the linear stage and
PM100D connected. The plot shows the measured calibration curve and the
currently planned points; enable the log power axis for a large dynamic range.
Use **Measure reference** when corrected absolute µW values are needed for the
current session. References are held in memory and are not silently restored
from older runs.

Select **Target power** to generate stage positions from numeric start/end/count
values with linear or logarithmic spacing, or provide a custom power list.
Target powers are corrected sample µW and require a valid measured session
reference. Position sweeps can be previewed as relative transmission when no
reference is available. Use **Use these stage positions** to copy the generated
list into the normal position input while retaining target-power provenance in
the resulting CSV rows.

## Experiment metadata and event journal

Every acquisition and processing workflow creates one portable
`*.experiment.metadata.json` sidecar. It contains requested settings, applied
and observed values, instrument identities, cached software commit/dirty
status, immutable settings snapshots, conditions, output file links, and the
terminal result. Spectrum and image CSV files remain the scientific data
artifacts; metadata also retains calibration and raw-detector provenance when
needed to explain how those artifacts were produced.

When a run reaches its first acquisition, the run also creates one append-only
`*.experiment.events.jsonl` journal. Capture attempts, warmups, repeats,
conditions, observations, processing, and late exports carry event IDs and
links to the experiment, acquisition, condition, settings snapshot, and output
file. The journal is written under one lock from worker threads and can be
read in bounded slices after cancellation or an interrupted write. LightField
and Andor integrations use the same journal; LightField-specific snapshot
fields remain in the sidecar for compatibility.

The metadata loader accepts older sidecars and fills additive fields without
connecting to hardware. External input references use portable names plus
content hashes, so equal basenames cannot silently merge. NumPy arrays are
serialized with dtype and shape and are never converted to truncated repr
strings. Unsupported ambiguous objects raise a serialization error so the
run can report incomplete metadata instead of claiming provenance.

Metadata schema 3 stores repeated objects and arrays of at least 256 serialized
bytes once, with file-local JSON pointers declared in `metadata_storage`.
Read full values through `ExperimentMetadataService.load_metadata(path)`;
plain `json.load()` exposes the stored pointers. The history index applies the
same encoding and its query API expands the values. Older metadata remains
readable, and legacy `.meta.json` / `.meta.txt` projections keep their formats.
Pointers never resolve external files or URLs.

Long `acquisition_schedule`, `all_points`, and `valid_points` lists
(64 or more uniform records) use the
`local-json-tables-v2` storage format. Shared settings and execution context are
stored once per column; changing values use compact value lists, dictionaries
or repeated runs. Nested gate values, exact floating-point values, ordering and
repeated group-instance identities are all preserved. The loader expands the
table to the same list of records used by the application; this does not change
the in-memory measurement schedule. Table-bearing sidecars use compact JSON
whitespace and a single buffered write before the existing fsync/atomic replace.
Plain JSON readers see the stored table,
so use the current metadata loader when reading these new sidecars. Existing
pointer-format and unencoded files remain readable and are not rewritten by
inspection. The history index uses the same lossless encoding.

MCD, Motion Sweep, and MegaSweep batch their final output associations into
one terminal manifest/history update. MCD performs that update in its existing
worker after hardware cleanup. Shared-run MCD2100 progress uses synchronous,
fsynced journal deltas; its legacy JSON is materialized at phase and terminal
boundaries. MegaSweep's per-map compatibility state stores point counts while
the complete exact plan remains in requested settings. BFP skips duplicate
metadata updates when the output path and exported bytes have not changed.
These changes do not queue pending writes in memory. Dual Gate uses the
file-boundary policy described above. Small compressed history files are expanded in a separate
reader process so their expanded size does not stall the GUI interpreter.

Spectrum exports retain one metadata record per output path. Repeating an
identical save leaves the metadata and event journal unchanged. Overwriting
that path with another frame refreshes its current settings and records the new
CSV content hash; it does not create another history row. A failed automatic
save retries the allocated destination, and an already saved reference is a
no-op. Detector temperature and status belong to capture events rather than
settings identity; a changing Andor temperature does not replicate calibration
snapshots. WinSpec events link to a recorded previous acquisition instead of
repeating its frame arrays.
# Motion Sweep condition batches

Motion Sweep supports an optional across-conditions run. Enter paired or
cartesian Doping/E-field (or Vtg/Vbg) arrays, choose independent Rot1 and Rot2
plans, gate-first or rotation-first ordering, and repeats, then review the
expanded count and optionally select/reorder entries in the sequence preview.
Each selected entry repeats the complete inner actuator sweep and is written to
its own uniquely suffixed CSV. The sequence emits an atomic JSON manifest with
complete, stopped, failed, and not-started entries. Numeric inputs accept
scalars, comma lists, `(start, stop, step)`, and the legacy `start:step:stop`
syntax. Keep-current rotation plans preserve the live angle without requiring a
rotation connection.

## Dual Gate execution order

The loop table defines values. In **Customized** mode, enabled rows with the
same Group number step together (Zip); their value counts must match. Different
groups are nested. **Synchronize** keeps each enabled row separate, and **Zip**
steps all enabled rows together.

The **Execution order** table places these groups around **Gate conditions**
(the enabled batch rows) and **Gate points** (the voltages within a row). Use the
Up/Down buttons to arrange levels from outermost to innermost. Gate points stay
inside their gate condition, and spectrum acquisition is always last. For
example, place a stage/exposure group first, then Gate conditions, Gate points,
and a paired RotIn/RotOut group. This measures every angle pair at a voltage
point before advancing the gate sweep. Move the angle group above Gate points
to measure a complete sweep at each angle pair instead.

RotIn means the excitation rotator; RotOut means the detection rotator. They are
ordinary optional loop parameters, not a mandatory polarization recipe. Disabled
axes are not moved or required. Enter your own calibrated numeric angles; the
app does not infer KK or KK-prime labels from an angle alone. Internal rotation
identifiers remain compatible with older saved configurations, while new
filename tokens use `RotIn` and `RotOut`.

Review the resulting sequence and counts, then use the single **Apply plan**
button in the fixed footer above Run. It applies the loop table, gate conditions,
and execution order together; **Discard** restores the last applied plan. The
footer identifies which parts have pending changes and keeps Run disabled until
they are applied or discarded. Invalid edits are highlighted with a reason.
The compact and full previews identify draft versus applied sequences. An
invalid draft cannot replace the applied plan with a runnable sequence.
For drafts above 20,000 loop combinations or potential spectra, live expansion
is deferred until Apply; both preview views explicitly identify the displayed
applied sequence. Filename previews use the same limit and ask for Apply rather
than expanding a large draft while typing. Resolved previews are reused when
their loop definitions, selections, and batch settings still match.
All entry points (Apply, restored sessions and Run) also enforce a separate total
planning budget: at most **100,000 loop contexts**, **100,000 potential spectra**
and **100,000 candidate planning steps**. Frames and repeats count toward this
budget; `When` filters do not bypass it because rejected candidates still cost
planning work. Oversized drafts remain editable, cannot replace the applied
plan and must be reduced using This run selections or split into smaller runs.
An oversized restored plan remains editable but cannot run. Valid plans between
the 20,000 live-preview threshold and the total budget can still be applied.
Apply reuses its resolved plan instead of constructing the full sequence twice.
Drag the divider between execution order and preview to adjust their space;
the Apply/Discard and Run controls remain outside the scrolling content.

If changing group membership invalidates an existing order, use **Reset order**,
review the new nesting, and Apply it; the app does not silently reuse a different
parameter under an old group label.
Existing saved plans retain their original loop/batch ordering. Interleaved
points remain grouped by their logical sweep and repetition in the output CSVs;
changing acquisition order does not require a separate CSV for every spectrum.

A batch row's **MeasurePower** selection requests a fresh corrected meter reading
for its output sweep and includes measured power in its filename in either
**PL** or **Ref** mode. A failed requested reading is reported as an error instead
of substituting a nominal or stale value. PL/Ref still identifies the measurement
mode, so select the appropriate mode for the experiment.

## Stage and rotation move verification

Stage and rotation adapters verify moves by default, including moves requested
from the instrument controls and measurement sweeps. Completion requires fresh,
consecutive position readings within tolerance. Readback retries do not issue
another move; a corrective move is permitted only after a fresh confirmation
that the device has stopped, with up to three correction retries allowed by default
within the same overall motion deadline. A final position-mismatch error reports
the retry count and remaining position error.

For ESP300 axes, the move allowance uses the current position, operating speed,
acceleration and deceleration, with a margin for settling and communication.
Slow moves can therefore exceed the former fixed 60-second wait. Unknown speed
uses a finite fallback allowance. Elliptec uses its synchronous library reply
with an explicit timeout, followed by genuine readback checks; the app does not
infer physical speed from its percentage-speed setting. No installed library
files are modified.

An unresolved move blocks acquisition and prevents an automatic follow-up move
while the device's stopped state is unconfirmed. This also applies to restore
moves during cleanup. A failed Elliptec move may require operator recovery and
reconnection because that adapter cannot independently confirm motion stopped.
Cancellation of an Elliptec library call is checked after the call returns;
it is not an immediate hardware stop.

Defaults are defined in `app/devices/motion_verification.py`, with tolerances
declared by each adapter (0.25 degrees for rotations, 0.01 mm for the Newport
linear stage, and 1 native stage unit for the Elliptec linear stage).
These tolerances are not automatically relaxed after failures.
