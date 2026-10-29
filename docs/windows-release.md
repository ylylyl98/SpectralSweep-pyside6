# Windows 自动测试、EXE 与 Release

工作流：`.github/workflows/windows-release.yml`。

- 向 `main` 推送：并行运行完整 `tests/` 测试集和 Windows EXE 构建；两者通过后自动发布带 run number 和提交 SHA 的预发布版本。
- 向 main 提交 PR：运行相同测试和构建，保留产物，不发布 Release。
- 推送 `v*` 标签：相同验证通过后发布正式 Release，并标记为 Latest。
- Actions 页面可手动运行；只有 main 或 v 标签运行允许发布。

构建使用 Windows x64、Python 3.13、PySide6 6.11.0、PyInstaller。
pip 下载缓存按三个 requirements 文件更新；构建和四组测试并行，发布等待全部成功。
测试文件按排序确定性分片，每个模块在独立 Python 进程和空白配置中运行，隔离 Qt 全局状态；
单模块超时或崩溃计为失败，剩余模块继续收集结果。每组包含清单与日志，不排除失败用例。
完整测试不使用 `continue-on-error`，保留 JUnit 中的失败和跳过原因。
部分原生 WinUSB 假传输测试需要另行提供已构建的测试运行时，现有 prerequisite skip 仍会显示。
云端不连接实验仪器，不能代替实际采集、温控、磁场运动或驱动兼容性验证。

Release 包含便携 ZIP 和 SHA256 校验文件。解压整个目录，运行 `SpectralSweep.exe`；
保留 `SpectralSweep-worker.exe` 和 `_internal`。目标电脑无需安装 Python，仍需按仪器安装
LightField、NI/VISA/DAQ、相机/USB/FTDI 驱动、指定的 attoDRY SDK 或 WinUSB 运行时。
用户配置保存在 `%APPDATA%/SpectralSweep`，开发者本机配置和实验数据不会进入构建。

打包验证先将产物复制到仓库外，再启动真正的 EXE，检查九个工作区、QML 资源、布局、
Python 回调错误及历史后台进程的 JSON 管道协议。测试报告和截图保留在 Actions artifacts。
同时验证 WinUSB 版本清单、页面重载需要的 Python 源资源，以及 watchdog 辅助程序入口。
main 的预发布不覆盖已有 Release，也不会替换正式版 Latest。

本机复现：

```powershell
python -m pip install -r requirements-ci.txt
python tools/run_ci_tests.py
python tools/build_exe.py --version local
```

正式发布示例（对已审查并在 main 的提交打标签）：

```powershell
git tag v1.0.0
git push origin v1.0.0
```

版本号仅为示例；发布前选择未使用的实际版本号。工作流失败时不发布，
在 Actions 页面查看测试 XML、构建日志和 `windows-exe-smoke-results`。
