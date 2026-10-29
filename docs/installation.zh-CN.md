# 安装、驱动与首次配置

[English](installation.md) | **简体中文** | [项目首页](../README.zh-CN.md)

只安装实际使用设备对应的组件。隔离界面预览不需要仪器软件；
EXE 已包含 Python 运行时和 Python 包，Windows 设备驱动及厂商应用需要另行准备。

## 选择安装方式

| 组件 | Setup / EXE 便携版 | 源码 / 开发 |
| --- | --- | --- |
| SpectralSweep | 推荐运行 Setup.exe，也可完整解压 Release ZIP | 取得仓库源码 |
| Python、PySide6、NumPy 等应用 Python 包 | 已包含 | 安装 Python 3.13 x64，再安装 `requirements-quick.txt` |
| Git | 不需要 | 推荐用于克隆与更新，也可以使用源码压缩包 |
| PyInstaller / pytest | 不需要 | 仅打包 / 测试时需要，通过 `requirements-ci.txt` 安装 |
| 仪器驱动、厂商软件及其要求的 .NET Framework | 按选用设备安装 | 与 EXE 相同 |
| ST133 私有迁移运行库及工具 | 不包含；迁移工具目前需要源码 + Python | 另行取得运行库 ZIP，按专用指南迁移 |

厂商下载可能需要账号、仪器序列号或已有软件授权。
请选择匹配设备和 Windows 版本的安装包；最新版 SDK 不一定适用于旧硬件。

<a id="installer"></a>
## Windows 安装器（推荐）

1. 打开 [GitHub Releases](https://github.com/ylylyl98/SpectralSweep-pyside6/releases)。
   main 构建标为 **Pre-release（预发布）**；正式版由 `v*` 标签发布。
2. 下载 `SpectralSweep-<版本>-windows-x64-Setup.exe` 和配套的 `.exe.sha256` 文件。
3. 如需校验，计算下载的安装器 SHA256，与配套文件比较：

   ```powershell
   Get-FileHash .\SpectralSweep-*-windows-x64-Setup.exe -Algorithm SHA256
   Get-Content .\SpectralSweep-*-windows-x64-Setup.exe.sha256
   ```

4. 完成采集及保存，正常关闭 SpectralSweep 和 worker。
   检测到任一进程仍在运行时，安装器会停止，不会替你结束仪器进程。
5. 运行 Setup。默认目录为 `%LOCALAPPDATA%\Programs\SpectralSweep`，
   只安装到当前 Windows 用户，应用安装本身不要求管理员权限。
   默认创建开始菜单快捷方式，可选桌面快捷方式；完成后不会自动启动程序。
6. 从快捷方式启动，再按下文完成实际设备的安装与配置。

升级时运行较新的 Setup，卸载时使用 Windows **已安装的应用**。
升级和卸载都保留 `%APPDATA%\SpectralSweep` 中的用户设置，也保留另存的采集数据；
厂商驱动/软件需要另行安装，可能有自己的管理员权限要求。
默认程序目录不作为实验输出目录，应另选可写的数据文件夹。

<a id="portable"></a>
<a id="exe-便携版"></a>
## EXE 便携版（备选）

1. 打开 [GitHub Releases](https://github.com/ylylyl98/SpectralSweep-pyside6/releases)。
   main 构建标为 **Pre-release（预发布）**；正式版由 `v*` 标签发布。
2. 下载 `SpectralSweep-<版本>-windows-x64.zip` 及对应的 `.sha256` 文件。
3. 可用下面的命令计算下载文件的 SHA256，与配套文件中的值比较：

   ```powershell
   Get-FileHash .\SpectralSweep-*-windows-x64.zip -Algorithm SHA256
   Get-Content .\SpectralSweep-*-windows-x64.zip.sha256
   ```

4. 将 ZIP **全部解压**到本机文件夹，运行 `SpectralSweep.exe`；保留同目录的
   `SpectralSweep-worker.exe` 和 `_internal`。
5. 连接设备前，完成下文中实际设备对应的安装与配置。

在解压目录打开设备断开的九工作区预览：

```powershell
.\SpectralSweep.exe --preview-workspaces
```

离线检查界面与后台进程，并保存报告和截图：

```powershell
.\SpectralSweep.exe --smoke-test "$env:TEMP\SpectralSweep-smoke"
```

这两种检查都不连接仪器；检查成功不代表驱动或真实采集已通过验证。

<a id="updates"></a>
## 升级与更新检查

**旧版需要先从官方 Releases 手动升级一次，才能获得应用内更新提醒。**
可安装新 Setup，或把新版 ZIP 完整解压到独立目录。保留已有配置及数据，
不要把 ZIP 覆盖解压到仍在运行的程序目录。

发布版中通过 **Help > Check for updates…**，或状态栏 **Updates** 打开更新窗口。
其中 **Update channel** 可选择 **Stable releases**（只检查正式版）或
**Include prereleases**（包含预发布）；**Check automatically (every 6 hours while running)**
控制自动检查，默认开启。程序启动后可检查一次，后续成功检查通常至少间隔 6 小时；
网络异常或 GitHub 限流可能延后检查，程序关闭时不会在后台检查。

预发布版默认包含后续预发布，稳定版默认只检查正式版，可在该窗口切换。
测量进行时自动更新提示会延后到空闲；手动检查可按需要打开。
检查不会中断采集或重新连接设备。

提醒仅显示版本信息并打开[本仓库官方 Release 页面](https://github.com/ylylyl98/SpectralSweep-pyside6/releases)，
不会下载或执行 Setup、安装文件、重启应用或 Windows。由你决定何时完成测量、
正常退出程序，再下载新 Setup 并安装。离线或检查失败不影响正常使用。

源码/本地构建继续通过 Git 更新，不使用 EXE 更新提醒。
先检查并提交或妥善保留本机改动，再获取/拉取目标分支，最后通过 `launch.bat` 重启。
升级时不要覆盖另一台电脑的仪器地址、SDK 路径和输出设置。

<a id="source"></a>
## 源码安装

安装 [Python 3.13 x64](https://www.python.org/downloads/windows/)。
请选当前 CI 使用的 **3.13 系列**，Python 官网默认展示的较新系列不等于本项目验证版本。
需要 Git 克隆和更新时，再安装 [Git for Windows](https://git-scm.com/install/windows)，
然后打开新的 PowerShell：

```powershell
git clone https://github.com/ylylyl98/SpectralSweep-pyside6.git
Set-Location SpectralSweep-pyside6
py -3.13 -m venv .venv-pyside6-313
.\.venv-pyside6-313\Scripts\python.exe -m pip install -r requirements-quick.txt
.\.venv-pyside6-313\Scripts\python.exe main.py --preview-workspaces
```

这个环境名称与 `launch.bat` 的识别规则一致；显式使用解释器路径，无需执行 PowerShell 激活脚本。
`requirements-quick.txt` 包含应用依赖，并固定已验证的 PySide6 6.11.0。

源码日常启动：双击根目录 `launch.bat`，或在 PowerShell 运行 `.\launch.bat`。
它创建/选择项目环境，并在 `requirements.txt` 变化时检查依赖。
`.\launch.bat --install` 仅检查/安装 Python 依赖；`.\launch.bat --update-deps` 会升级依赖。
升级后需要回到固定 Qt 版本时，重新安装 `requirements-quick.txt`。

`python main.py --mock` 模拟光谱仪，但保留常规应用运行环境；
完全隔离、禁止设备操作的预览应使用 `--preview-workspaces`。
开发测试与打包命令见[首页](../README.zh-CN.md#开发与验证)。

## 仪器依赖与官方下载

以下要求同时适用于 EXE 和源码用户，另有说明的情况除外。
在 SpectralSweep 中选择设备前，应能在 Windows 或对应厂商工具中确认设备身份。

<a id="lightfield"></a>
### Princeton Instruments / LightField

- 取得 [LightField](https://www.teledynevisionsolutions.com/en-ca/products/lightfield/?model=LF&segment=tvs&vertical=tvs-princeton-instruments)
  及 Automation 组件，安装匹配相机和光谱仪的驱动。
- 若系统尚未具备兼容环境，启用/安装该 LightField 安装器要求的
  [.NET Framework](https://dotnet.microsoft.com/en-us/download/dotnet-framework)。
  安装 Python 的 `pythonnet` 包不能代替厂商应用或这个 Windows 运行环境。
- 检查 Windows 环境变量 `LIGHTFIELD_ROOT`，它必须指向这台电脑上的实际 LightField 安装根目录。
  代码还会查找其中的 `AddInViews`，并加载名称包含 V5 的 LightField Automation 程序集。
  如果安装器未设置该变量，在当前用户的 Windows 环境变量中添加它，然后重启程序及启动它的终端。
- 先在 LightField 中建立可工作的相机/光谱仪实验配置，再在 SpectralSweep 中选择配置及输出光路。

单独的 PICam SDK/驱动不提供本后端使用的 LightField Automation 接口。
从另一台电脑复制 DLL 不等于完成厂商软件安装。

<a id="andor"></a>
### Andor SDK2 相机与 Shamrock

通过 [Andor SDK 与驱动入口](https://andor.oxinst.com/products/software-development-kit/software-development-kit)
取得实际相机和光谱仪匹配的驱动及 SDK2/Shamrock 组件。
本程序已支持的 CCD/InGaAs 后端使用 SDK2；只有 SDK3 的安装不等价。

项目的 `andor dll/` 和 EXE 的 `_internal/andor dll/` 已包含部分 DLL，
这些文件不负责安装 USB/PCI 相机或光谱仪的设备驱动。
在 **Settings** 核对 SDK2 和 Shamrock DLL 目录，以相机序列号与型号绑定 Si/InGaAs 用途，
不要假设另一台电脑的枚举顺序仍然有效。先验证厂商工具能识别设备，再关闭占用设备的应用后连接。

<a id="visa"></a>
### VISA 仪器、Keithley SMU 与 GPIB

安装 [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)
或兼容的系统 VISA 实现，以及实际 USB/串口/GPIB 转接器的驱动。
NI GPIB 控制器可能还需要 [NI-488.2](https://www.ni.com/en/support/downloads/drivers/download.ni-488-2.html)。

当前控制器使用 PyVISA 默认的系统资源管理器。
应用包含的 `pyvisa`、`pyvisa-py` Python 包不会替你安装 NI-VISA 或 GPIB 内核驱动。
先用 NI MAX 或对应厂商工具检查设备发现，再在程序中选择本机实际 VISA 地址和通道用途；
运行前核对仪器的输出与限流设置。

<a id="pm100d"></a>
### Thorlabs PM100D

从 [Thorlabs 功率计软件页面](https://www.thorlabs.com/software-pages/PM100x)取得驱动/工具，
并准备 [VISA 运行环境](#visa)。当前 GUI 通过 **PyVISA/SCPI** 通信；
仓库中遗留的 `TLPMX.py` 和 `TLPMX_64.dll` 属于另一套接口。

OPM 可能将设备设为较新的 TLPMX 驱动。
如果功率计在 OPM 中可用，却不能作为 VISA USB 仪器列出，按
[官方 OPM 手册的 Driver Switcher 章节](https://media.thorlabs.com/contentassets/012d23129bc54ff2bfdbf4c6d4a306fa/tl_opm_v7.0.pdf?v=1117095710)
选择本 GUI 使用的 **NI-VISA/PM100D** 驱动路径。
驱动选择也会影响其他使用该功率计的程序；切换时关闭这些应用，
之后在 VISA 工具及 SpectralSweep 中确认 USB 资源与序列号。

<a id="mff101"></a>
### Thorlabs MFF101 翻转镜

安装匹配设备的 [FTDI D2XX 驱动](https://ftdichip.com/drivers/d2xx-drivers/)
或对应 Thorlabs 驱动包。本后端使用 pylablib 的直接 FTDI 接口，不要求 Kinesis 应用或其 .NET SDK。

扫描/选择八位 USB 序列号，核对物理位置与光路映射。
MFF101 是可选设备，自动路由**默认关闭**。没有翻转镜时保持
**Settings > Instruments > MFF101 light paths > Use MFF101 automatic routing on this PC** 未勾选。
沿用旧配置时也要检查这一项并保存配置。

<a id="motion"></a>
### Elliptec 与 Newport 位移/旋转

- **Elliptec：**安装 USB 转接器的虚拟串口驱动，通常为 [FTDI VCP](https://ftdichip.com/drivers/vcp-drivers/)。
  本适配器通过 `COMx` 串口通信，与 MFF101 的直接 D2XX 接口不同。
- **Newport ESP300/ESP301：**准备 [VISA](#visa)，以及实际 USB、RS-232 转接器或 GPIB 接口的驱动。
  ESP301 原生 USB 需安装 [Newport](https://www.newport.com/p/ESP301-3G) 提供的匹配驱动，
  参考[官方 USB 驱动安装步骤](https://www.newport.com/medias/sys_master/npresources/hb5/h6a/9954466168862/ESP301%20-%20USB%20Driver%20Installation%20Manual/ESP301-USB-Driver-Installation-Manual.pdf)。

选择正确的控制器型号、地址、轴号和波特率。
程序中 ESP301 原生 USB 默认 921600，RS-232 默认 19200；仍应与实际控制器配置一致。
这条 ASCII/VISA 通信路径不使用 Newport .NET SDK。
连接 Newport 轴会启用其电机，连接前阅读[位移说明](newport-rotation.md)。

<a id="magnet"></a>
### attoDRY1000 / APS100 与 attoDRY2100

**attoDRY1000 / APS100：**从 [attocube 支持](https://www.attocube.com/en/ressources/services-and-support)
取得匹配的虚拟串口驱动，并准备 [VISA](#visa)。
本后端直接连接 APS100，不使用 attoDRY2100 SDK；选择实际地址及该磁体经确认的配置参数。

**attoDRY2100：**仓库中有 Python SDK 副本 `CRYO2100/`，EXE 中位于 `_internal/CRYO2100/`。
确认它与仪器固件兼容；需要其他版本时向 [attocube 支持](https://www.attocube.com/en/ressources/services-and-support)获取。
将 `attodry2100.sdk_directory` 改为本机实际 SDK 目录，并设置本系统的 `host`、`channel`。
加载器要求目录中有导出 `Device` 的 `__init__.py`，或该目录下有 `atto_device/__init__.py`。
旧配置中的开发电脑盘符路径不是新电脑上可直接使用的位置。
EXE 用户加载兼容的 SDK 副本时，无需另装 Python 解释器。

SDK 目录是高级 JSON 配置项。先关闭 SpectralSweep，再编辑
`%APPDATA%\SpectralSweep\config.json`，将需要的字段合并到已有的 `attodry2100` 对象中，保留其他设置。例如：

```json
{
  "attodry2100": {
    "sdk_directory": "C:\\SpectralSweep\\_internal\\CRYO2100",
    "host": "192.0.2.10",
    "channel": 0
  }
}
```

这是配置片段，不能替换整份配置文件。请把示例路径、仅用于文档的 IP 地址和通道改为本机目录及实际仪器设置。

<a id="daq"></a>
### NI DAQ 模拟输入/输出

**只有实验装置使用 NI DAQ 硬件时**，才需要安装
[NI-DAQmx](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html)。
在 NI MAX 中确认设备名和物理通道名。
应用包含的 Python `nidaqmx` 包不等于 NI-DAQmx 系统驱动；
只通过 VISA 使用 Keithley，并不因应用包含这个 Python 包就需要安装 DAQmx。

<a id="esp32"></a>
### ESP32 成像位移台

控制器要求使用 [`firmware/esp32_stage`](../firmware/esp32_stage) 的 `stage-v2` 固件。
USB 驱动按实际开发板选择，参考
[Espressif ESP32-S3 串口连接说明](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/get-started/establish-serial-connection.html)。
开发板可能使用 USB-UART 桥接，也可能使用原生 USB，不能统一指定一种芯片驱动。

[PlatformIO](https://docs.platformio.org/en/latest/core/installation/index.html)
仅在编译/刷写固件时需要；使用已配置开发板的普通 EXE 用户无需安装它。
上传及连接时选择本机真实端口，替换固件项目中的示例 COM 口。
确认固件握手后，按照[位移台配置步骤](esp32-imaging-stage-trial.md)设置参考位置和行程。

<a id="winspec"></a>
### WinSpec / ST133 InGaAs

两条采集路径需要分别准备：

| 路径 | 用户需要已有的环境 |
| --- | --- |
| 旧版远程 WinSpec / 实验性 PVCAM | 远程相机电脑/虚拟机中的匹配厂商驱动/运行库和 Python 2.7 相机服务。WinSpec 路径另需授权 WinSpec 与 COM；实验性 PVCAM 服务直接调用厂商 DLL。配置可达主机和端口。见[侧口配置](winspec-side-port.md)与 [PVCAM 后端](pvcam-backend.md)。 |
| 本地 Windows 11 WinUSB | 私下提供的 `ST133-InGaAs-Win11-x64-v6.zip`、指定设备接口登记及本机绑定。WinUSB 由 Windows 提供，厂商/助手运行库不在公开 EXE 中。按[新电脑步骤](st133-new-pc-setup.md)及 [Microsoft WinUSB 说明](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/winusb-installation)操作。 |

目前文档中的本地安装与验证流程需要**源码 + Python 3.13 x64**；普通 EXE 安装无需 Python。
源码流程不会部署或验证 EXE 独立的运行库/状态目录，完成该流程不等于 EXE 的 WinUSB 部署已可用。
独立的本地探测器测试不需要 XP、WinSpec GUI 或 LightField；
**集成到 SpectralSweep 的 InGaAs 流程仍用 LightField 管理光谱仪及输出光路**。

本地 WinUSB 为实验性支持，限于文档指定硬件、手动一维采集及曝光/累积范围，
不是其他相机的通用驱动替换方案。通过实验室已有的私下传递流程取得运行库，不要在公开 Release 中寻找该 ZIP。

<a id="first-run"></a>
## 首次配置

1. **确认设备身份。**在 Windows 设备管理器和厂商工具中核对型号/序列号，连接前释放其他应用占用的设备句柄。
2. **配置这台电脑。**按实际设备检查探测器后端与光学配置、SDK 目录、相机序列号、
   COM/VISA 地址、轴号、波特率，以及磁体主机/通道。
3. **设置保存与样品。**选择可写输出目录和 Sample ID。
4. **检查可选光路切换。**无 MFF101 时关闭自动路由；有翻转镜时按真实光路核对映射。
5. **保存并验证。**保存本机配置，先检查一次小规模采集及保存的 CSV/元数据，再开始较大流程。

设置保存在 `%APPDATA%\SpectralSweep\config.json`。
源码首次启动可能把仓库 `config.json` 作为兼容默认值导入；
应检查其中与电脑有关的值，而不是复制另一台电脑的整份配置。
程序启动不会自动重新连接仪器或恢复测量。

通知为可选功能，可在程序中配置本机 ntfy 订阅，无需安装设备驱动；
见[通知设置](usage-reference.md#per-pc-ntfy-subscriptions)。
简短中文启动说明见[此处](../00_启动说明.txt)。

## 常见问题

| 现象 | 优先检查 |
| --- | --- |
| EXE 启动缺 DLL/模块 | Setup 安装可在关闭程序后重跑对应安装器；ZIP 检查是否完整解压，主 EXE、worker 与 `_internal` 是否仍在一起。 |
| Setup 提示主程序或 worker 仍在运行 | 完成采集及保存，再正常退出；安装器不会结束仪器进程。 |
| 旧版没有检查更新菜单 | 先手动安装一次新版 Setup，见[更新检查](#updates)。 |
| LightField 无法加载 | 厂商安装、Automation 程序集、要求的 .NET Framework、`LIGHTFIELD_ROOT`；修改环境变量后重启启动进程。 |
| PM100D 在 OPM 中可用，但 VISA 找不到 | Driver Switcher 的驱动选择及 VISA 安装，见 [PM100D](#pm100d)。 |
| USB/COM/VISA 中没有目标设备 | 设备/接口驱动、设备管理器、真实序列号/地址及厂商工具；安装 Python 包不能代替驱动。 |
| attoDRY2100 SDK 目录不存在 | 将旧电脑路径改成本机兼容的 `CRYO2100`/厂商 SDK 目录。 |
| 源码启动出现 Qt 版本错误 | 使用预期环境的 Python，并安装 `requirements-quick.txt`。 |

在 [Issues](https://github.com/ylylyl98/SpectralSweep-pyside6/issues) 反馈时，
附上构建的 `version.json`、Windows 版本、设备/后端、连接方式、复现步骤和错误信息。
界面、模拟或 CI 验证成功不代表仪器调试验收已经完成。
