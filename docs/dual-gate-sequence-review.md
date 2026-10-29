# Dual Gate sequence 预览设计与验证

本次将 Quick 模式的 Dual Gate 序列审查迁到独立模型和 QML 视图。
参数编辑、计划应用、执行 worker 和数据保存仍沿用现有逻辑。

## 预览方法的选择

本项目需要回答“实际下一步采什么”“为什么跳过”“相邻步骤改变了什么”。
适合作为默认界面的是 **执行层级 + 当前步骤 + 下一步变化**。
这是根据本项目工作流作出的设计判断，尚未经过操作员对照实验。

| 方式 | 能回答的问题 | 本次选择 |
| --- | --- | --- |
| 当前执行路径 | 各循环如何嵌套、每层当前处于何值 | 默认；外层 → 内层逐层展示，Repeat/Point 明确计数 |
| 执行顺序表 | 任意第几步的条件、参数、电压、采集数量和状态 | Full view 按需展开；宽窗口分列，窄窗口分行显示 |
| 下一步变化 | 此步完成后改变什么、哪个内层循环回到起点 | 默认显示；完整窗口另有相对上一项的详情与 When 规则 |
| 参数随执行步变化的轨迹 | 电压跳变、重复访问、切换密度 | 适合作为后续辅助视图；本次未实现，不把步号当真实时间、不平滑电压跳变 |
| Vtg/Vbg 或 doping/field 覆盖图 | 二维覆盖、重复点、遗漏区域 | 适合作为后续辅助视图；单独使用会丢失顺序、repeat 和其他循环维度 |

“先概览、再缩放/过滤、按需详情”的依据来自
[Shneiderman 的原始论文](https://www.cs.umd.edu/~ben/papers/Shneiderman1996eyes.pdf)。
Qt 的 [ListView 文档](https://doc.qt.io/qt-6/qml-qtquick-listview.html)说明按视口实例化
delegate；[Qt Quick 性能建议](https://doc.qt.io/qt-6/qtquick-performance.html)强调先测量
实际瓶颈，避免不必要的处理。本次在此基础上额外限制每页数据量，视图只接收模型。
文档网址会随 Qt 发布更新；实际验证运行时是 PySide6 6.11.0，项目最低要求为 6.9。

## 已实现的行为

- 使用已有编译 schedule 的原始顺序，展示层不重新求解 When，不写回计划。
- 主页面显示当前/选中步骤的真实外层→内层路径，以及下一步变化；内部仍只读取最多
  8 项和相邻步骤。Full view 每页最多 200 项，可按执行步号跳转。
- 路径显示当前参数值、条件、Repeat i/n、Point i/n 和采集动作。同组参数明确标为
  Linked settings。下一步说明最外层变化及能从实际索引证明的内层回到起点。
- Previous/Next 按单步浏览，浏览时标为 Selected step；Back to run 恢复跟随。
  进度刷新不会强制跳走用户正在检查的步骤。
- 嵌套任务的一步是一张光谱；旧式任务的一步是一个 sweep，明确显示 frames × repeats。
  光谱数量和文件数量分开计数。
- 旧式计划可显示被 When 跳过的候选组合，跳过行没有执行编号。嵌套计划只展示真实
  可执行任务和规则摘要，不为说明过滤而展开额外笛卡尔积。
- 详情显示条件、循环参数、电压、采集数量、路径、When、相对上一执行步的变化。
- 隐藏预览不格式化新页；打开完整窗口使用独立分页模型，关闭即释放。
- 页面重载保留页码、选择、跟随和显示选项；完整窗口未关闭时沿用重载保护。
- 新 QML 预览支持浅色/深色；紧凑布局去掉重复的 Widgets 标题和完整序列入口。

大计划 Apply 已改为后台构建，保留现有执行顺序、When 条件、安全电压检查及数量限制。
参数快照在界面线程取得，后台只处理数据；输入改变会使旧结果失效。计划与统计结果一起
安装，构建期间禁用 Run/Apply，关闭窗口先取消任务并在退出后继续关闭。

旧式计划的候选索引仍需 O(执行步骤数) 构建，但 Apply 将这项工作移入后台，界面接收
不可变索引。嵌套计划只读取当前页和相邻项；进度通知合并到最多 20 Hz，结束/失败立即
显示。隐藏预览不刷新。长参数列表使用有上限的解析缓存和延迟生成的选择标签。
自动草稿预览仅构建小计划；大草稿明确提示 Apply 后查看，不限制实际计划的大小。
这些机制减少界面线程工作，并不把整份执行计划变为惰性数据；后台编译仍需时间和内存。

## 查看方式

```powershell
.\.venv-pyside6-313\Scripts\python.exe main.py --preview-quick
```

打开 **Dual Gate sequence** 标签。Interleaved gate points 示例包含 90 张光谱、
18 个逻辑文件；Sweeps with When filtering 示例包含 9 个 sweep 任务和 3 个跳过组合。
这是临时配置下的合成示例，不创建真实控制器。

正式界面使用 `main.py --ui quick`；Dual Gate 参数页中同样提供此预览。
`--ui widgets` 继续使用原树预览；以当前启动配置选择前端。

## 初版验证记录

```powershell
.\.venv-pyside6-313\Scripts\python.exe -m pytest tests/test_dual_gate_sequence_model.py tests/test_quick_dual_gate_sequence.py tests/test_quick_dual_gate_integration.py -q
.\.venv-pyside6-313\Scripts\python.exe tools/verify_dual_gate_sequence.py --native --output tmp/dual-gate-editor
.\.venv-pyside6-313\Scripts\python.exe tools/validate_quick_bundle.py --native --output tmp/quick-dual-gate-bundle
```

- 新模型、QML、实际参数页集成：19 项通过，包含 100%/150%/200% 原生缩放、深浅主题、
  10 万步有界读取、键盘导航、手动浏览、隐藏更新、关闭释放及重载状态。
- 相关草稿、计划预算、旧预览、嵌套审查、页面重载、预览启动、资源包与运行状态回归：
  82 项及 3 项子测试通过。两组共 101 项及 3 项子测试，并非整个仓库测试全集。
- 实际 PresetsPanel 使用完整应用样式与临时配置渲染；90 步计划、底部 Apply/Run/Stop
  可见、无 QML 警告。1400×1000 可用；请求 1000×800 时旧编辑器仍将宽度约束为
  1250，右侧内容需要滚动。这不是已完成的小屏幕重排。
- 仓库外源码副本原生运行通过，共 144 项资源；示例序列页也在副本中验证，禁止导入
  设备控制器、SDK、通知和 watchdog。此项不等同于独立 EXE 验证。
- 未连接真实仪器、未运行真实长测；科学图表和统一设备服务仍需后续迁移。

截图与机器可读报告保存在本机 `tmp/dual-gate-editor-quick-final/` 和
`tmp/quick-dual-gate-bundle-final/`，不作为实验数据或版本资源提交。

## 2026-10-27 执行路径与长序列响应

主视图改为执行路径、当前步骤和下一步变化，保持固定进度/ETA/操作区。实际 Windows
Direct3D11 的 1920×1080 与 1024×768 明暗主题四张截图通过：无横向滚动/越界、布局
稳定，Qt/QML 警告、Python 回调错误及设备导入尝试均为零。截图采用真实编译的 60 点
计划和模拟进度，未调度采集；报告位于 `tmp/sequence-route-live/report.json`。

长计划的工作量现在分为三部分：

- 自动草稿预览最多 2,000 个潜在测量点，超出提示 Apply 后预览；实际计划预算不变。
- Apply 后台完成编译、电压安全校验、需求/时间统计、文件名首个适用上下文和 flat
  索引。快照代数与内容校验拒绝过期结果，关闭/重载保护线程生命周期。
- GUI 安装只接收已验证数据；运行更新合并到最多 20 Hz，终态立即显示。紧凑视图
  不创建执行列表 delegate，Full view 仍有界加载；100k flat 索引安装只读 ≤12 项。

本机分别以单独进程运行 100k 计划、可见 Quick 面板和 10ms Qt 定时器：

| 计划 | Apply 返回 | 后台至安装完成 | GUI 安装 | 最长心跳间隔 |
| --- | ---: | ---: | ---: | ---: |
| Nested：1000 rotation × 100 gate points | 41.6 ms | 5.406 s | 51.2 ms | 169.4 ms |
| Flat：100000 rotation × 1 gate point | 127.9 ms | 1.944 s | 44.5 ms | 89.9 ms |

两组均无 QML 警告。心跳测量不包含首个定时器触发前的 Apply 返回耗时，故两者分列。
报告和可复跑脚本位于 `tmp/sequence-route-live/dual_gate_async_apply_benchmark*`。
这是隔离 offscreen 性能测试，不保证每台电脑的延迟上限；Python 后台仍有短暂 GIL/GC
停顿，整体编译并未消失，也不代表真机长测已经验证。

独立编辑基准中，100k 参数未改变时的草稿检查约由 829ms 降到 29ms；实际更值的冷路径
约 93ms。10k 草稿取消即时全量编译后约 68ms。数字会受后台负载影响，不应混用冷/热
路径或把所有输入操作宣称为恒定时间。

最终组合回归：模型、实际 QML/页面集成、异步 Apply、解析/选择性能、计划预算、运行
启动/ETA、值选择、interleaving、long-run 与 nested review 共 **167 passed，4 subtests
passed / 125.31s**。包含 100%/150%/200% 原生缩放、100k 有界读取、密集进度合并、
隐藏恢复、后台取消/关闭、过期输入拒绝、索引复制与真实 worker 执行顺序不变。
独立布局回归另有 **18 passed / 20.19s**；本轮没有运行整个仓库测试全集。
重载、文件名元数据和预览边界另组 **32 passed / 26.39s**。旧 filename 测试替身补上
新的 group metadata 方法绑定，继续使用真实预算/选择校验，未放宽越界或 When 断言。
