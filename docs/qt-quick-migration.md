# Qt Quick 渐进迁移记录

目前默认入口使用 Qt Quick 前端，保留 `--ui widgets` 兼容入口。9 个工作区的
参数、表格、图表、参考光谱和业务弹窗已经接入 Quick 展示；共享主题覆盖深浅模式。
原有 QWidget 窗口容器和隐藏控件仍作为实验逻辑的兼容状态，这不是纯 QML/独立设备
服务的架构重写。真机长期采集和独立 EXE 安装仍需现场验证。

## 五步执行结果与边界

| 步骤 | 已落实 | 尚未通过的门槛 |
| --- | --- | --- |
| 1. 建立基线 | 迁移前 Spectrum、取消、历史性能、会话、布局共 102 项测试及 7 项子测试通过；记录旧 Motion 表格基准；提供可重复的渲染/事件延迟验证工具 | 真实设备和长期采集性能需要在实验条件下测量 |
| 2. 抽取接口 | 新增独立 CommandPresenter、RuntimeState/WorkflowState、HistoryViewModel、MotionSequenceModel；稳定属性、命令白名单、资源预留和停止代际保护 | 一些历史执行 worker 仍在页面模块；Stage/Rotation 的同步/raw-adapter 使用路径尚未统一为新的异步设备服务 |
| 3. 低风险页面 | QML 历史页：样品/类型筛选、搜索、防抖、50 条分页、20 条事件分页、过期请求丢弃、明确加载/错误/空状态和键盘操作 | 当前为 QQuickWidget 宿主 |
| 4. 逐页迁移 | 9 页及仪器侧栏的标量表单、原验证逻辑支持的表格编辑、参考谱列表、科学图表、业务弹窗、历史和双栅/Motion 序列使用 Quick；源控件保留身份 | QWidget 仍提供分组/滚动/窗口容器；原生文件和颜色选择对话框保留系统行为 |
| 5. 主窗口选择 | 默认 Quick；显式 Widgets 回退；全工作区隔离预览、Qt版本门槛、资源白名单和跨目录预览验证 | 不删除兼容入口；未验证真机长时间采集和独立安装包 |

## 使用与验证

项目解释器示例：`.venv-pyside6-313/Scripts/python.exe`。普通 `python` 在不同电脑
可能指向另一个版本；本轮使用 Python 3.13.9 / PySide6 6.11.0。

```powershell
python main.py --preview-quick
python main.py --preview-workspaces
python main.py --ui quick
python main.py --ui widgets
python tools/benchmark_motion_sequence.py --help
python tools/verify_quick_ui.py --native --output tmp/quick-native
python tools/verify_quick_ui.py --native --frontend widgets --output tmp/widgets-native
python tools/validate_quick_bundle.py --help
python tools/profile_quick_startup.py --native --mode preview --output tmp/quick-startup.json
python tools/verify_dual_gate_sequence.py --native --output tmp/dual-gate-editor
python tools/verify_quick_workspaces.py --native --output tmp/quick-workspaces
python tools/run_quick_plot_regressions.py --suite all
```

`--preview-quick` 先于配置、通知、watchdog 和真实控制器导入分支，使用临时输出、
临时历史、独立 QSettings 和 `PreviewSpectrumController`。不会把生产配置用于预览。
原生渲染验证使用 Windows Qt 平台和 `WA_DontShowOnScreen`，输出截图、事件间隔和
取消后没有迟到帧的检查报告。该测量包含模拟器与测试计时开销，不代表真实采集速度。

跨目录工具将明确白名单的源码/资源复制到仓库外的临时目录，以 `python -I`
运行验证；应用模块必须来自副本，禁止导入设备控制器、SDK、通知和 watchdog。
它复用当前解释器的第三方依赖，因此证明资源路径可迁移，不等于生成了独立 EXE，
也不验证厂商驱动安装。

## 首轮 Windows 原生模拟预览基线

同机、同解释器，两个前端顺序各运行一次，使用隐藏 Windows 窗口和默认渲染后端。
`window_ready_ms` 包含窗口构建和固定 150 ms 等待，不是渲染线程的首帧时间。
Quick 预览还会创建历史页和 65 条合成记录，因此这组数据只比较完整预览入口，
不能单独归因于 QML 渲染开销，也不能当作真机采集吞吐量。

| 指标 | Widgets 预览 | Quick 预览 |
| --- | ---: | ---: |
| 窗口就绪（含固定等待） | 961.02 ms | 2438.55 ms |
| 10 ms 测试定时器实际间隔中位数 | 15.34 ms | 24.44 ms |
| 实际间隔 P95 | 57.98 ms | 77.12 ms |
| 实际间隔最大值 | 86.61 ms | 95.50 ms |

两者均产生 4 帧模拟数据、停止后无迟到帧、未导入真实设备控制器；Quick 无 QML
警告。首轮测量没有证明整体提速，当时保留 Widgets 默认。这是历史基线，不是当前版本的性能结论。可用上方命令复测；
本机报告位于 `tmp/widgets-final-native/report.json` 和 `tmp/quick-final-native/report.json`。

## 后续优化：Dual Gate 预览与启动

Dual Gate 的实际编译计划现在直接驱动 `DualGateSequenceModel`，不在视图中重新
求解或修改执行顺序。主页面每页 8 步，完整窗口每页最多 200 步；只有可见页转换为
展示数据。隐藏时只保留最新计划引用，重新显示时再刷新。新视图支持手动浏览、跟随
当前步、When 跳过候选、外层到内层结构、每步电压与相邻变化；重载可恢复浏览状态。
设计取舍、准确性边界和测试说明见 [Dual Gate 序列预览](dual-gate-sequence-review.md)。

隔离预览的第三个标签首次打开才创建 Dual Gate 示例。启动分析发现，旧入口用
65 次独立历史 upsert 构建模拟数据，其中重复连接/提交占据了明显时间。现用一个
事务写入这批临时索引，保留同格式 sidecar；同一临时目录重复打开可复用完整样例，
缺失时重建。真实实验历史的写入和持久化策略没有因此改变。

同机 Python 3.13.9 / PySide6 6.11.0、Windows 隐藏窗口、默认 Quick 后端，各取
三个独立进程的中位数：

| 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 窗口构建至首次同步截图 | 1811.10 ms | 1328.67 ms |
| Quick 预览装配阶段（包含 QML 与模拟历史构建） | 712.68 ms | 247.38 ms |

前一指标缩短约 26.6%。它不含模块导入，也不含首轮基线的固定 150 ms 等待，不能
与上表的 `window_ready_ms` 直接对比；不是渲染线程真正呈现首帧的时间，也不证明
正式应用启动或真机采集提速。数据保存在 `tmp/quick-startup-{before,after}-preview-{1,2,3}.json`。

后续全页迁移新增 `Theme.qml` 和 Basic 控件、标量表单、虚拟化表格、科学图表与
参考谱列表，并重组页面分区。当前视觉/滚动检查见 [布局审查](qt-quick-layout-review.md)，
绘图数值契约与限制见 [科学图表迁移](qt-quick-scientific-plots.md)。

## Motion 表格基准

数据来自同一解释器、每个规模独立进程的单次 offscreen 测量。
这里比较旧 `QTableWidget` 与新 `MotionSequenceModel` + `QTableView`，
不是 QML `TableView` 的构建速度。两个前端共享新模型；QML 虚拟化另有交互测试。
RSS 增量从输入行已分配后开始计算。旧版没有运行 10 万行。

| 行数 | 旧构建时间 | 新构建时间 | 旧 RSS 增量 | 新 RSS 增量 |
| --- | ---: | ---: | ---: | ---: |
| 1,000 | 0.6716 s | 0.0686 s | 57.29 MB | 6.23 MB |
| 5,000 | 7.6602 s | 0.1583 s | 254.49 MB | 9.44 MB |
| 100,000 | 未测 | 1.8171 s | 未测 | 85.02 MB |

10 万行勾选约 0.1765 ms，末行移到首行约 0.7189 ms。初始输入仍需一次深拷贝，
大规模构建仍同步进行；本轮移除了每个单元格复制整行、统计选中时导出全部行和
移动一行重建整张表的工作。QML 只接收模型引用，delegate 可复用；测试要求
10 万行视图创建的 delegate 小于 1,000，后续勾选和移动不深拷贝行载荷。

## 保留的行为

- QML 命令只在白名单内调用已有页面的验证/worker 启动路径，重新检查按钮和资源可用性。
- Spectrum 参数应用、采集及延迟取消清理期间保持占用；停止请求不等于硬件已停止。
- 输入错误阻止启动，但不阻止仍可用的 Stop/Abort；状态刷新不重建正在编辑的字段。
- 运行草稿、应用计划、读回值和保存数据继续遵守原逻辑；不把显示归一化写入原数据。
- 光谱真实坐标、断轴、共享 Y、手动缩放、参考谱和二维图由 Quick 数值绘图适配器呈现；保留原数据/坐标策略。
- 长计划主预览最多 8 步、详情每页最多 200 步、隐藏详情不刷新的已有约束保留。
- 历史加载绑定打开时的页面、样品和类型；切换上下文或运行中拒绝应用过期设置。
  BFP 同一页面内部切换类型也立即使旧上下文失效，切回后不会恢复旧加载权限。
- 页面重载保持设备控制器身份；QML surface 加载失败时保留该页的标准控件并报告。
- Motion 的 source index、执行顺序、repeat、Apply 冻结结果和 Cancel 不修改调用者数据保持不变。
- Motion 预览在确认、取消和异常退出后释放对话框、模型与 Quick 场景；应用共享一个 QML 引擎缓存组件，避免逐窗口重建引擎。

## 后续纯 QML 切换条件

逐页将参数/计划编辑及运行编排迁入独立服务，消除对隐藏 Widgets 控件的适配。
当前已对曲线、断轴、二维图、坐标标定及原有科学视图测试执行 Quick 分支回归；后续继续验证厂商设备
连接/采集/停止/关闭、跨设备互锁、长时间运行和配置恢复。所有目标通过后才替换
主窗口并删除旧视图。当前混合版不能被视作上述真机验证已完成。

## 首轮验证记录

- Quick、运行状态、模型、生命周期、启动及跨目录资源组合：106 项通过。
- 旧 Spectrum、取消、会话、布局、页面重载、历史性能、Motion 等回归组：164 项和 7 项子测试通过。
- Windows 原生隐藏预览及仓库外源码副本验证通过；140 项资源，QML 无警告。
- Motion Quick 在 100%、150%、200% 缩放下验证深浅主题、按钮边界和虚拟化。
- 未连接真实设备、未运行实际实验；未构建或验证独立 EXE。

组合测试曾暴露本机 PySide6 6.11 的线程包装对象生命周期问题：对短生命周期
`RuntimeState` 调用 `thread()` 后，`shiboken6.dump` 显示主线程包装对象的父对象
改为该状态；垃圾回收后线程对象失效，后续嵌套事件循环可能原生崩溃。现改为通过
生命周期稳定的 `QCoreApplication.instance()` 检查 GUI 线程，并新增独立子进程
回归：反复回收状态 20 次，确认主线程仍有效且事件循环可运行。原失败组合已完整通过。
