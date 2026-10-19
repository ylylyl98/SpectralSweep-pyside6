# ST133 / InGaAs 迁移到 Windows 11 x64

适用于把**已验证的同一台 ST133 控制器和 InGaAs 探测器**搬到另一台 Intel/AMD Windows 11 电脑。不是通用 ST133 驱动：原生助手仍会核对控制器及探测器 EEPROM，身份不符会停止。PIXIS/A026 不属于本流程。

交付分为两部分：

- GitHub：SpectralSweep 源码、原生助手改动、安装工具和 SHA-256 清单。
- 私下复制：`ST133-InGaAs-Win11-x64-v6.zip`，包含 44 个经过校验的助手、厂商 x86 DLL、数据文件及私有运行库。不要上传这个 ZIP 到公开仓库。

内核驱动使用 Windows 自带的 `winusb.inf` / `WinUSB.sys`。无需复制本机 System32、DriverStore 或整套 WinSpec 安装目录，也不需要安装 XP 或运行 WinSpec GUI。64 位 Python 通过独立的 32 位助手使用这些 DLL。

## 1. 准备软件

安装 Git 和 **Python 3.13 x64**，使用短的英文路径。以下 PowerShell 命令均在仓库根目录运行；不要把 ZIP 放到运行库内部。

```powershell
git clone https://github.com/ylylyl98/SpectralSweep-pyside6.git C:\SpectralSweep
Set-Location C:\SpectralSweep
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m tools.winspec.migrate_winusb install --zip C:\Transfer\ST133-InGaAs-Win11-x64-v6.zip
```

安装工具逐项核对仓库清单，拒绝损坏、重复、多余或越界条目。验证后才发布到 `.runtime/winusb-ingaas/runtime-v6-portable`。已有运行库不会被覆盖；此步骤不打开相机。安装失败时，独立的 `runtime-staging-*` 目录可能保留供检查，不能作为运行库使用。

## 2. 在新电脑选择 WinUSB

接通 ST133 电源和 USB；如果运行 VMware，先让宿主 Windows 持有 ST133。关闭会占用该 ST133 的采集会话。

```powershell
.\.venv\Scripts\python.exe -m tools.winspec.migrate_winusb devices
```

记下输出的完整 `InstanceId`，例如 `USB\VID_0BD7&PID_A010\...`。不同电脑或 USB 插口可能不同，**不要复制旧电脑的地址**。如有多台 A010，按设备管理器的设备实例路径确认实际搬来的这一台。

在设备管理器中，核对这一项的硬件 ID 包含 `VID_0BD7&PID_A010`。如果已经使用 WinUSB，可跳过驱动选择。否则对**这一项**选择：更新驱动程序 → 浏览我的电脑 → 让我从可用驱动列表中选取 → 通用串行总线设备 → WinUsb Device。名称可能随系统语言变化；找不到该项时停止并记录设备状态，不要改动 PIXIS，也不要禁用驱动签名。

这是微软提供的手动安装路径，参见 [WinUSB installation](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/winusb-installation)。工具不自动替换 PnP 驱动。

## 3. 登记接口并绑定本机地址

仅下面的 `register-interface` 需要**管理员 PowerShell**。进入同一个仓库，替换示例地址：

```powershell
Set-Location C:\SpectralSweep
$st133Instance = 'USB\VID_0BD7&PID_A010\把这里替换为 devices 的实际后缀'
.\.venv\Scripts\python.exe -m tools.winspec.migrate_winusb register-interface --instance $st133Instance
```

该命令确认指定设备已使用 WinUSB，并只在其 `Device Parameters` 中添加专用接口 GUID `{42D96BC8-048E-4A31-BD2E-16903596F637}`。原有 GUID 会保留，修改前的值写入 `.runtime/winusb-ingaas/interface-backup-*.json`。检测到正在运行的助手或未解决的采集所有权时拒绝修改。

按工具提示将 **ST133 的 USB 拔插一次，插回同一个口**。随后回到普通 PowerShell，再设置 `$st133Instance`，执行：

```powershell
$st133Instance = 'USB\VID_0BD7&PID_A010\把这里替换为 devices 的实际后缀'
.\.venv\Scripts\python.exe -m tools.winspec.migrate_winusb bind --instance $st133Instance
.\.venv\Scripts\python.exe -m tools.winspec.migrate_winusb check
```

`bind` 保存本机 `.runtime/winusb-ingaas/device-binding.json`，其中包含精确设备地址和便携版清单哈希。它不上传到 Git。助手启动时会回报选定地址，桥接层核对后才发送采集请求。错误绑定不会自动回退到另一台相机。

`check` 仅检查文件、绑定和 Windows PnP/接口登记状态，**不代表实机采集已通过**。实际接口是否可打开、设备 EEPROM 是否符合，仍由下面的采集测试确认。

换 USB 插口后先重新运行 `devices`。如果地址改变，停止采集并正常断开，将旧 `device-binding.json` 移为一个新的备份文件名，确认原位置已没有该文件，再按新地址登记、绑定。工具不会覆盖不同的已有绑定。不要在测量中改绑定或删除 `active.json`。

## 4. 先做三帧探测器测试

无需遮光，也不需要连接 LightField。测试只控制 ST133，不控制光路、波长、制冷或温度设定。输出目录必须是新目录：

```powershell
.\.venv\Scripts\python.exe -m tools.winspec.migrate_winusb capture --frames 3 --exposure-ms 100 --output C:\SpectralSweep-tests\first-three
```

逐帧保存 CSV、原始计数和元数据，确认保存后登记缓存回收资格，最后正常关闭助手。成功会生成 `result.json`，包含三帧记录及 `normal_close_verified: true`。任何帧或关闭阶段失败都会返回错误，不自动重试或杀进程。保留错误输出、测试目录和 `.runtime/winusb-ingaas` 的诊断记录。

像素为零并不能单独证明驱动或 CCD 有问题。这个测试验证迁移后的通信、回执、保存与正常关闭；不验证温度、光学响应、波长标定或绝对无错帧。

## 5. 接入 SpectralSweep

```powershell
.\.venv\Scripts\python.exe main.py
```

目前应用中的 InGaAs 配置仍需要 **LightField 管理光谱仪和输出光路**。新电脑若要使用完整界面，需要另行安装该设备匹配的 LightField/光谱仪软件，并创建正确的实验配置；这些不在相机迁移 ZIP 中。

**MFF101 翻转镜是可选设备**，新配置默认关闭其自动光路切换。没有 MFF101 的电脑无需连接翻转镜或安装其驱动；若沿用了已启用自动切换的旧配置，在 **Settings > Instruments > MFF101 light paths** 取消勾选 **Use MFF101 automatic routing on this PC**，再点击 **Save config to disk**。这不会关闭 LightField 对光谱仪输出光路的管理。

选择 InGaAs + LightField 配置，将 **InGaAs acquisition** 改成 **Local WinUSB — manual acquisition (experimental)**，再连接设备。在 Spectrum 中设定曝光与累积并使用 Acquire 1D / Save current。不要沿用 XP 的远程采集地址。不要复制旧电脑的整个 `config.local.json`，应重新选择本机设备和路径。

当前范围为整数 **10–1000 ms 曝光、1–100 次软件累积、512 × 1 原始像素**。同一曝光持续复用连接；变曝光会正常关闭并重新初始化。温度、制冷、自动扫描和本地波长标定尚未开放。先完成新电脑的三帧测试，再验证日常曝光、累积和保存；原电脑上的长测不等于新电脑已经通过长测。

## 维护与复原

本机旧 v5 安装没有绑定文件时仍使用原先的 v5；安装 ZIP 不会切换现有会话。要复原现有电脑，先正常断开，保留便携绑定的副本，再移出该绑定文件，旧运行库仍在。只有原先验证过 v5 的原电脑适用这个回退方法，新电脑不能靠删除绑定使用旧地址。

迁移包不是 DLL 的公开再分发版本。重新打包可用 `python -m tools.winspec.migrate_winusb bundle --source <已验证的运行库> --zip <新的本地ZIP>`。包必须匹配当前仓库的 `app/devices/winusb_portable_runtime.json`。

原生重编译由 `tools/winspec/build_persistent_winusb.py --portable` 完成，仍需要本地保存的冻结 `usb_compat` 基线、私有厂商运行库和编译器；普通安装不需要编译。这些原始基线并未作为一个可公开独立重建的 SDK 发布。变更原生代码后必须重新构建、测试、审查并更新清单，不能只替换单个 DLL。

## 本次便携包验证（2026-10-14）

- 原生构建完成：新电脑地址选择器通过 1,408 项检查；模拟传输完成 1,000 帧，并覆盖错误退出、EOF、日志轮转和曝光读回。模拟测试没有打开物理 USB。
- 发布源码副本：相机、迁移、缓存、保存及 Spectrum 相关测试 240 项通过，22 项因需要另行提供原生模拟运行库等条件跳过；原生曝光/会话另测 33 项通过，日志轮转另测 8 项通过。
- 使用最终 ZIP 安装到独立目录，在原电脑的同一 ST133 上采集三帧 100 ms，验证导出、保存确认和正常关闭。原 v5 安装、驱动、PIXIS 及已有测量进程保持不变。
- 尚未在第二台电脑验证；通信成功不等于温度或光学帧新鲜度已经验证。实测数据和完整诊断日志保留本机，公开测试使用合成数据。
- 补充检查覆盖 Git 的 LF/CRLF 差异：清单按规范化 JSON 内容计算身份，运行库文件仍逐字节校验 SHA-256。相关绑定与迁移测试 24 项通过。
- 另一个 WinSpec/LightField 回归组 118 项通过，1 项旧的 20 ms 超时提示测试出现计时敏感失败；在未修改的原版提交 `6861771` 上也复现同一失败。本次没有改动该 LightField 超时实现，不能将此回归组报告为全通过。
