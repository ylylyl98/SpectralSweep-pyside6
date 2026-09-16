# Prepare / Start telemetry handoff 集中审查

## 最终结论：PASS

Astra 已独立完成定向复查，前述 4 项阻塞问题和 2 项小修正均已收束；本次 handoff 范围内没有剩余阻塞发现。验证全程使用 offscreen Qt、真实 controller 的请求入口和 gated fake adapter，不使用真实 App、仪器或优化模式 `-O`，所有断言保持启用。

独立证据按最后相关修改分段保留，未重复运行无关套件：

- 原 4 个正向交接场景通过：background、monitor、manual 双 getter、client timeout 均在实际 owner drain 后只启动一次，参数保持快照，随后执行独立 fresh preflight，worker 期间不追加 display 请求。
- 定向复查通过关闭后两入口、shutdown 旧 token、external busy 恢复、runner 构造异常回滚、thread.start 异常回滚、Prepare recovery 可复核而 Start 仍拒绝、waiting Prepare 按钮禁用，以及 watchdog 回调中读取 ownership snapshot 无锁反转。snapshot 中已 drain 的旧 timeout 枚举仅在正常 terminal bookkeeping 前保守延迟，不会让未 drain 请求提前启动。
- 最后恢复修改的 3 个新增 unittest 独立运行通过：`test_cancel_waiting_does_not_enable_saved_false_polling_before_drain`、`test_terminal_worker_does_not_fallback_enable_saved_false_polling`、`test_saved_display_state_is_discarded_on_generation_change_disconnect_and_shutdown`（3 tests，OK）。
- `mcd2100_handoff_restore_generation_probe.py` 独立运行 exit 0：断开后恢复记录已清除；真实 fake-controller 重连 generation 1→2 后 polling 保持 True，旧 monitor 未重新启动。
- 最后的同步共享锁重入修正经源顺序核对：`had_restore_state` 在 `_thread_finished` 第一条可执行语句捕获，早于任何释放锁信号。`mcd2100_handoff_restore_reentrant_probe.py` 独立运行 exit 0，结果为 `polling_after_terminal=false`、`saved_state_left=false`、`interlock=false`。这覆盖 MainWindow 同步调用 source `set_externally_busy(False)` 提前消费恢复记录的路径。

最后两份临时正向探针位于 `C:/Users/commo/AppData/Local/Temp/`。较早记录中的 defect reproduction / 失败探针结果保留为历史，不作为最终通过证据。Luna 另报告受影响套件 175 项通过及后续定向回归、编译和差异检查通过；这些不冒充 Astra 独立重跑结果。

交接修复没有修改 worker/adapter 采样实现，不增加扫描中的 getter；用户实机行为仍需由后续正常使用确认，本审查没有进行硬件验证。

## 第一轮结论：需要定向修复

Astra 独立审查，Luna 实现。仅检查本次 handoff 与受影响生命周期；不更改 worker 采样、SDK 参数或磁场策略。未启动 App、连接设备或执行真实仪器操作。

正向真实 controller / gated fake adapter 探针通过 background、monitor、manual 双 getter、client timeout 四种场景：所有已接受 getter 自然 drain 后启动一次；等待期间参数保持快照；没有追加 display 请求；测量 worker 期间 polling 关闭。

复现脚本：

- `C:/Users/commo/AppData/Local/Temp/mcd2100_handoff_review_probe.py`
- `C:/Users/commo/AppData/Local/Temp/mcd2100_handoff_lock_review_probe.py`

## 集中修复清单

1. **P1：snapshot / watchdog 锁顺序反转。** `pending_work_snapshot()` 在 controller lock 内调用获取 request lock 的 `get_state()`；`_caller_timeout` 顺序相反。有界探针确认锁环。使用 controller lock 内的 registry、display slot 身份与 `drained_future.done()`，避免嵌套 request lock。验收 watchdog 并发、terminal 未 drain、客户端超时未 drain 三条路径。
2. **P1：idle 快路绕过生命周期资格。** `shutdown()` 完成后的排队 Start 仍执行 fresh preflight。两个入口、handoff 及 idle/waiting 消费使用一致检查：closing、连接/断开过程、worker/thread/intent、外部占用、真实 control pending 和各 operation 的 recovery 规则。拒绝后终结已创建 metadata。验收关闭后两入口/旧 token 零请求，mixed 请求拒绝，正常 idle 启动一次。
3. **P2：取消与失效丢失 display 恢复状态。** external busy 撤销 intent 后 polling 永久关闭；Cancel 无条件开启 polling；monitor 原状态未保留。捕获原 polling/monitor，集中处理收尾与延后恢复。实际 owner 未 drain 保留互锁；同 generation、连接有效且无关闭/recovery/占用时恢复原状态；断开/关闭不恢复。覆盖原 True/False、Cancel、外部占用解除、control 冲突解除及启动失败。
4. **P2：launch 构造异常残留 worker。** `_Runner` 构造失败时存在 worker、没有 thread，导致永锁。构造、信号连接和启动前步骤必须可回滚；仅清理本次未运行对象，终结 metadata 并走统一恢复；不得调用未启动 worker 的 run/cancel/cleanup。实际已启动者保留真实生命周期。验收 runner 构造及 thread.start 前失败、零 SDK Stop、可再次启动。

同时修正：waiting Prepare 不应因 recovery 标志被一律撤销，应保留 fresh recovery 检查；Start 仍拒绝 recovery。Prepare 按钮在已有等待 intent 时禁用。

Luna 按上述清单集中修复和相关自检，Astra 只复查修复与受影响路径；最终通过结论待记录。

## Start 和采样行为澄清

`MCD2100Worker.run()` 已在温控、光学和 gate 配置之前调用 `MagnetPreparation.prepare()`。单独 Prepare 是可提前执行的操作；正常 Start 自带准备。`owner work is still draining` 原缺陷在进入 worker 前拦截，未执行到自动准备。模式恢复标志是单独的 Start 拒绝条件。

本次未更改 `MCD2100Worker._leg()`。扫描仍以 field-only 读取为主；原有温控选项启用时，每张光谱前后各读取一次 sample 温度。这不是 telemetry 修复新增的逐点查询。
