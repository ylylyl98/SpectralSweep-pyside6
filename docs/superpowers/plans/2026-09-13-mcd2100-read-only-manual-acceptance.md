# MCD2100 只读功能：用户实机验收

由用户在代码审查通过后执行。Agent 不启动 App、不连接仪器、不使用 computer-use。本轮不验证扫速写入。

## 记录日志（可选）

在项目目录的 PowerShell 中执行以下命令启动 App，并将本轮诊断写入本地 `mcd2100-read-test.log`。仅为这次人工验收启用；平时启动不默认生成此文件。命令不自动点击连接。

```powershell
.\.venv-pyside6-313\Scripts\python.exe -c "import logging, runpy; logging.basicConfig(filename='mcd2100-read-test.log', filemode='a', level=logging.WARNING, format='%(asctime)s %(name)s %(levelname)s %(message)s'); logging.getLogger('controllers.attodry2100_controller').setLevel(logging.DEBUG); logging.getLogger('app.devices.attodry2100_adapter').setLevel(logging.DEBUG); runpy.run_path('main.py', run_name='__main__')"
```

## 简短步骤与预期结果

1. 确认仪器空闲、App 没有测量或其他设备任务，再手动连接。必要状态查询完成后应自动读取一次 Current 和 Default，显示完成或具体错误，**不弹表格窗口**。
2. 点击“查看扫速表”，记录两张表的 channel、原始 index、range/rate、读取时间、耗时及完整错误信息。查看只展示缓存，不新增设备请求。
3. 让 telemetry 更新数轮，重复普通刷新、切换 tab、再次查看缓存。扫速表读取次数应保持不变；界面应可响应，磁体和温度分别显示原始数据年龄。
4. 空闲时点击一次“刷新扫速表”。应只有一次新的完整读取；连续点击不重复排队。读取结束或超时调用实际返回后，telemetry 应恢复。记录刷新前后更新时间和任何等待/错误文字。
5. 如时间合适，可自行安排断线重连验证：断线缓存标为上次连接／过期；新连接自动尝试一次。新读失败时，旧表不能被标为本次有效。无需为验收强行断线、Stop 或制造设备超时。

## 发回哪些结果

- 两张表的原始内容或清晰截图，包括标题、时间和错误；不要自行换算单位或将某个 index 命名为 Fast。
- 本地 DEBUG 日志（如已启用），并复制 MCD2100 的 App 日志区内容：完整刷新周期和扫速表汇总记录位于 App 日志区。记录首次自动读取及手动刷新的起止时间/耗时、调用次数。
- 读取期间 UI 是否卡顿、磁体/温度年龄如何变化、结束后 telemetry 是否恢复；异常时附界面原文和发生步骤。
- 是否做过重连、外部软件是否改变过设置；没做的项目标记“未验证”。

Agent 将依据这些记录区分队列等待与 SDK 耗时，分析结果并修复问题。尚未实测的设备性能、单位、index/Fast 映射、写入允许状态、生效时机、持久性及读回验证均保持未确认；读取成功不代表写入条件已经满足。

## Prepare / Start 交接修复后的补充观察

这项观察在交接修复审查通过、由用户择时重新运行新版 App 后进行；不要求为验证额外启动一次实验。在下一次正常计划的 Prepare 或 Start MCD 操作中，若后台 telemetry 正在读取，应先显示等待当前读取结束，再启动已请求操作一次，而不是把正常读取报为 `owner work is still draining`。记录等待文字、等待时长和随后阶段日志。

等待时的取消只撤销尚未启动的意图；若实验已经启动则沿用原运行取消流程。测量期间应暂停独立显示刷新，扫描线程的字段采样不改变。已有温控选项开启时每张光谱前后的 sample 温度记录属于原逻辑，不是这次新增查询。
