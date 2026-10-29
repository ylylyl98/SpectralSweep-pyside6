# SpectralSweep

[English](README.md) | **简体中文**

[![Windows 测试与 EXE](https://github.com/ylylyl98/SpectralSweep-pyside6/actions/workflows/windows-release.yml/badge.svg?branch=main)](https://github.com/ylylyl98/SpectralSweep-pyside6/actions/workflows/windows-release.yml)

用于光谱采集及电压、位移、磁场联动测量的 Windows 桌面程序，采用 PySide6 和 Qt Quick。

[下载 Windows EXE](https://github.com/ylylyl98/SpectralSweep-pyside6/releases) ·
[安装与官方下载](docs/installation.zh-CN.md) ·
[反馈问题](https://github.com/ylylyl98/SpectralSweep-pyside6/issues)

![SpectralSweep Qt Quick 双栅工作区，预览设备处于断开状态](docs/screenshots/spectralsweep-quick.png)

*截图来自隔离预览，使用模拟设置；设备未连接，测量操作已禁用。*

## 主要功能

- 采集、比较和导出 LightField、Andor Si/InGaAs 及已支持 WinSpec 配置的光谱。
- 用嵌套循环、批次条件和逐步序列预览规划 Dual Gate 双栅测量。
- 组织 2D 电压扫描、位移扫描、BFP 和 MCD 测量流程。
- 统一管理样品设置、运行历史、CSV 数据和实验元数据。
- 使用深浅主题；不连接仪器即可隔离预览全部 9 个工作区。

## 快速开始

| 使用目的 | 需要准备什么 |
| --- | --- |
| 直接使用桌面程序 | 从 **Releases** 下载便携 ZIP，已包含 Python、Qt 和应用需要的 Python 包。 |
| 连接真实仪器 | **只安装自己实际使用设备对应的驱动和厂商软件**，见下表。 |
| 从源码运行或开发 | **Python 3.13 x64** 和仓库源码；推荐使用 Git 更新。 |
| 配置已支持的 ST133 WinUSB 后端 | 私下提供的运行库 ZIP；目前文档中的安装/验证流程使用源码和 Python。 |

### 使用 Windows EXE

1. 打开 [Releases](https://github.com/ylylyl98/SpectralSweep-pyside6/releases)。main 自动构建版本标为 **Pre-release（预发布）**。
2. 在 **Assets** 下载 `SpectralSweep-<版本>-windows-x64.zip` 及对应的 `.sha256` 文件。
3. **完整解压**后运行 `SpectralSweep.exe`，保留同目录的 `SpectralSweep-worker.exe` 和 `_internal`。
4. 连接仪器前，完成[首次配置检查](docs/installation.zh-CN.md#first-run)。

发布包面向 Windows x64；本项目记录的实验室部署环境为 Windows 11 x64。
普通 EXE 用户无需另装 Python、Git、pip、PySide6 或 PyInstaller。

仅浏览界面、保持仪器断开时，在解压目录中运行：

```powershell
.\SpectralSweep.exe --preview-workspaces
```

### 使用源码

先安装 [Python **3.13 x64**](https://www.python.org/downloads/windows/) 和
[Git for Windows](https://git-scm.com/install/windows)，再执行：

```powershell
git clone https://github.com/ylylyl98/SpectralSweep-pyside6.git
Set-Location SpectralSweep-pyside6
py -3.13 -m venv .venv-pyside6-313
.\.venv-pyside6-313\Scripts\python.exe -m pip install -r requirements-quick.txt
.\.venv-pyside6-313\Scripts\python.exe main.py --preview-workspaces
```

源码日常启动使用仓库根目录的 `launch.bat`，它会选择项目虚拟环境，并在 Python 依赖变化时检查安装。
`--mock` 模拟光谱仪；`--preview-workspaces` 才是设备操作被禁用的隔离预览。
详见[源码安装](docs/installation.zh-CN.md#source)。

## 用户需要自行下载安装什么

**Python 包或 DLL 文件不能代替 Windows 设备驱动、厂商软件、匹配的 SDK，以及厂商要求的 .NET 运行环境。**
请按实际设备选择条目；[安装指南](docs/installation.zh-CN.md)列出了每项的官方下载入口、配置步骤和检查方法。

| 设备 / 后端 | 用户另行安装或取得 | 配置指南 |
| --- | --- | --- |
| Princeton Instruments + LightField | 有效授权的 **LightField + Automation 组件**、相机/光谱仪驱动，以及该版本要求的 .NET Framework。单独 PICam 不能替代 LightField Automation。 | [LightField](docs/installation.zh-CN.md#lightfield) |
| Andor SDK2 + Shamrock | 匹配相机/光谱仪的设备驱动，按需安装 SDK2/Shamrock 组件；发布包中的 DLL 不能代替设备驱动安装。 | [Andor](docs/installation.zh-CN.md#andor) |
| Keithley / VISA 仪器 | **NI-VISA** 或兼容的系统 VISA 实现及 USB/GPIB 接口驱动；NI GPIB 可能还需 NI-488.2。 | [VISA](docs/installation.zh-CN.md#visa) |
| Thorlabs PM100D | VISA 兼容设备驱动和 VISA 运行环境。若 OPM 将设备设为 TLPMX，用 Driver Switcher 切换到本 GUI 使用的 **NI-VISA/PM100D** 路径。 | [PM100D](docs/installation.zh-CN.md#pm100d) |
| Thorlabs MFF101 | 匹配设备的 FTDI **D2XX** 驱动；本后端不要求 Kinesis 应用或 .NET SDK。 | [MFF101](docs/installation.zh-CN.md#mff101) |
| Elliptec / Newport ESP300–301 | Elliptec 的 USB **VCP/COM** 驱动；Newport 需要 VISA 和匹配的 USB/串口/GPIB 驱动。 | [位移与旋转](docs/installation.zh-CN.md#motion) |
| attoDRY1000 / APS100 | 设备供应商提供的匹配虚拟串口驱动，以及 VISA 运行环境。 | [磁体](docs/installation.zh-CN.md#magnet) |
| attoDRY2100 | 与固件兼容的 Python SDK。项目和 EXE 已附带 `CRYO2100` 副本，不匹配时获取厂商对应版本；需设置本机实际目录。 | [磁体](docs/installation.zh-CN.md#magnet) |
| NI DAQ 硬件 | **仅使用 NI DAQ 模拟输入/输出时安装 NI-DAQmx 系统驱动**；只用 VISA 控制 SMU 不需要因此安装它。 | [DAQ](docs/installation.zh-CN.md#daq) |
| ESP32 成像位移台 | 本项目的 `stage-v2` 固件和开发板实际使用的 USB 串口驱动；PlatformIO 仅在编译/刷写固件时需要。 | [ESP32](docs/installation.zh-CN.md#esp32) |
| WinSpec / ST133 InGaAs | 已工作的旧版远程相机服务，或按源码流程部署的私有 WinUSB 运行库及设备绑定；完整集成仍用 LightField 管理光谱仪。 | [WinSpec](docs/installation.zh-CN.md#winspec) |

## 首次配置

1. 先确认 Windows 与对应厂商工具能识别设备。
2. 在 **Settings / Instruments** 选择本机设备、SDK 路径和连接地址。
3. 设置可写输出文件夹和 Sample ID，核对光路映射；没有 MFF101 时保持可选自动路由关闭。
4. 保存配置，连接目标设备，先验证一次小规模采集及保存结果，再开始扫描。

用户配置位于 `%APPDATA%\SpectralSweep\config.json`。
换电脑后应重新选择 COM/VISA 地址、SDK 路径和网络设置。
详见[首次配置与故障排查](docs/installation.zh-CN.md#first-run)。

## 文档导航

| 主题 | 文档 |
| --- | --- |
| 安装、官方下载与故障排查 | [中文](docs/installation.zh-CN.md) · [English](docs/installation.md) |
| 详细操作、数值语法、样品设置及元数据 | [操作参考](docs/usage-reference.md)，保留原有中英文章节 |
| Dual Gate 序列与多仪器配置 | [序列预览](docs/dual-gate-sequence-review.md) · [多配置测量](docs/dual-gate-multi-setup.md) |
| 光谱比较与采集恢复 | [光谱比较](docs/spectrum-comparison.md) · [LightField 恢复](docs/lightfield-capture-recovery.md) |
| 位移与旋转设备 | [Newport](docs/newport-rotation.md) · [ESP32](docs/esp32-imaging-stage-trial.md) |
| WinSpec / ST133 | [远程侧口配置](docs/winspec-side-port.md) · [Windows 11 迁移](docs/st133-new-pc-setup.md) |
| Qt Quick 与自动发布 | [界面迁移](docs/qt-quick-migration.md) · [布局审查](docs/qt-quick-layout-review.md) · [Release 流程](docs/windows-release.md) |
| 维护与历史资料 | [工具说明](tools/README.md) · [参考文件](docs/reference/README.md) |

## 开发与验证

当前 Windows CI 基线为 **Python 3.13 + PySide6 6.11.0**。

```powershell
.\.venv-pyside6-313\Scripts\python.exe -m pip install -r requirements-ci.txt
.\.venv-pyside6-313\Scripts\python.exe tools/run_ci_tests.py
.\.venv-pyside6-313\Scripts\python.exe tools/build_exe.py --version local
```

[GitHub Actions](https://github.com/ylylyl98/SpectralSweep-pyside6/actions/workflows/windows-release.yml)
把完整测试集按模块隔离并分成四组，与 Windows 打包并行运行。
测试与实际 EXE 迁移启动检查全部通过后才发布；检查覆盖 9 个工作区、资源、后台进程入口及页面重载源码。
缺少先决条件的测试会显示跳过原因。CI 不验证真实采集、温控或磁场运动。

推送 `main` 后自动生成带版本号的**预发布版**；`v*` 标签生成正式版，见[发布说明](docs/windows-release.md)。

反馈问题时请在 [Issues](https://github.com/ylylyl98/SpectralSweep-pyside6/issues) 附上
构建的 `version.json`、设备/后端、连接方式、复现步骤和相关错误信息。
