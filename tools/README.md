# 工具目录说明

**日常打开 SpectralSweep，请双击项目根目录的 [`launch.bat`](../launch.bat)。**
本目录保存启动辅助程序、诊断工具和开发工具。

| 文件或目录 | 用途 |
| --- | --- |
| [`../launch.bat`](../launch.bat) | Windows 桌面程序的日常启动入口；通常只需使用它。 |
| [`launch_app.py`](launch_app.py) | 由 `launch.bat` 调用，检查依赖并启动根目录的 `main.py`。 |
| [`diagnose_keithley2400.py`](diagnose_keithley2400.py) | Keithley 2400 仪器诊断工具。 |
| `winspec/run_*.cmd` | WinSpec / PVCAM 专项诊断或远端相机服务入口；部分脚本会访问相机、采集数据。具体用途见脚本内说明。 |
| `winspec/update_*.cmd` | WinSpec 桥接服务维护、更新脚本。 |
| [`embed_app_icon.py`](embed_app_icon.py) | 开发时生成应用图标资源。 |

`winspec/` 中的脚本可能依赖同目录辅助文件、XP 上的 Python 或
`C:\WinSpecRemote`。请保持目录结构，按对应的
[WinSpec 说明](../docs/winspec-side-port.md)或
[PVCAM 说明](../docs/pvcam-backend.md)使用。
