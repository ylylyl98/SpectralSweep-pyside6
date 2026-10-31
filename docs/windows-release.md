# Windows 自动测试、Setup 安装器与 Release

工作流：`.github/workflows/windows-release.yml`。

用户安装与设备驱动清单：[中文](installation.zh-CN.md) · [English](installation.md)。

- 向 `main` 推送：并行运行完整 `tests/` 测试集和 Windows EXE/Setup 构建；全部验证通过后自动发布带 run number 和提交 SHA 的预发布版本。
- 向 main 提交 PR：运行相同测试和构建，保留产物，不发布 Release。
- 推送 `v*` 标签：相同验证通过后发布正式 Release，并标记为 Latest。
- Actions 页面可手动运行；只有 main 或 v 标签运行允许发布。

构建使用 Windows x64、Python 3.13、PySide6 6.11.0、PyInstaller 和 Inno Setup 编译器。
pip 下载缓存按三个 requirements 文件更新；构建和四组测试并行，发布等待全部成功。
测试文件按排序确定性分片，每个模块在独立 Python 进程和空白配置中运行，隔离 Qt 全局状态；
单模块超时或崩溃计为失败，剩余模块继续收集结果。每组包含清单与日志，不排除失败用例。
完整测试不使用 `continue-on-error`，保留 JUnit 中的失败和跳过原因。
部分原生 WinUSB 假传输测试需要另行提供已构建的测试运行时，现有 prerequisite skip 仍会显示。
云端不连接实验仪器，不能代替实际采集、温控、磁场运动或驱动兼容性验证。

Release 包含四个文件：

- `SpectralSweep-<version>-windows-x64-Setup.exe`：推荐的按用户安装器。
- 同名 `.exe.sha256`：Setup 校验文件。
- `SpectralSweep-<version>-windows-x64.zip`：便携版。
- 同名 `.zip.sha256`：ZIP 校验文件。

Setup 默认安装到 `%LOCALAPPDATA%\Programs\SpectralSweep`，不要求管理员权限；
提供开始菜单、可选桌面快捷方式和卸载，升级沿用固定应用标识。
安装/卸载前检测主程序与 worker，要求用户先正常完成测量并退出，
不强制终止仪器进程、不自动启动应用或重启系统，不删除用户配置和实验数据。
便携版需完整解压，保留 `SpectralSweep-worker.exe` 和 `_internal` 后运行主 EXE。
两种方式的目标电脑都无需安装 Python，仍需按仪器安装
LightField、NI/VISA/DAQ、相机/USB/FTDI 驱动、指定的 attoDRY SDK 或 WinUSB 运行时。
用户配置保存在 `%APPDATA%/SpectralSweep`，开发者本机配置和实验数据不会进入构建。

打包验证先将产物复制到仓库外，启动真正的 EXE，在独立子进程中检查正常主窗口的真实依赖导入
（不创建控制器或连接设备），再检查九个工作区、QML 资源、布局、
Python 回调错误及历史后台进程的 JSON 管道协议。测试报告和截图保留在 Actions artifacts。
同时验证 WinUSB 版本清单、页面重载需要的 Python 源资源，以及 watchdog 辅助程序入口。
之后编译 Setup，用独立验证身份、临时安装目录和隔离配置做静默安装，
核对版本及完整文件清单，运行安装后 EXE 的相同 smoke；
再启动专用空闲 worker，确认 Setup 拒绝安装且不终止该 worker，
最后验证覆盖升级、卸载应用文件和保留配置标记。验证安装不覆盖用户的真实安装注册项。
任一步失败都会阻止该次 Release。日志和安装后 smoke 也保留在 `windows-exe-smoke-results` 中。
main 的预发布不覆盖已有 Release，也不会替换正式版 Latest。
重复运行若发现同名 Release 已存在会明确失败并保留其资产，避免把缺少文件的既有发布误判为成功。

本机复现：

```powershell
python -m pip install -r requirements-ci.txt
python tools/run_ci_tests.py
python tools/build_exe.py --version local
python tools/build_installer.py
```

构建 Setup 需安装 Inno Setup 6.3 或更新版本，或通过
`python tools/build_installer.py --compiler "C:\path\to\ISCC.exe"` 指定编译器。
GitHub Windows runner 使用其预装编译器；普通应用用户不需要这些开发工具。

`version.json` 保留 `version`、`commit`、`python`，并提供
`schema_version`、整数 `build_number`、`channel`、`repository` 和 UTC `built_at`。
CI 的构建号来自 `GITHUB_RUN_NUMBER`，可用 `--build-number` 显式指定；
`v*` 默认为 `stable`，`build-*` 为 `preview`，本机构建为 `local`，可用 `--channel` 指定。
用户可用 **Help > Check for updates…** 或状态栏 **Updates** 检查更新，
并在同一窗口选择稳定/预发布通道及运行期间每 6 小时的自动检查。
检查只提示并打开官方发布页，不下载、安装或重启；测量期间延后自动弹窗。
旧版需要手动升级一次才能获得此功能，源码仍通过 Git 更新。
详见[中文用户说明](installation.zh-CN.md#updates)与[English guide](installation.md#updates)。

正式发布示例（对已审查并在 main 的提交打标签）：

```powershell
git tag v1.0.0
git push origin v1.0.0
```

版本号仅为示例；发布前选择未使用的实际版本号。工作流失败时不发布，
在 Actions 页面查看测试 XML、构建日志和 `windows-exe-smoke-results`。
