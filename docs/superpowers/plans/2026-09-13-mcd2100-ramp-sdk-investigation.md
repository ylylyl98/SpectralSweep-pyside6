# 扫速表读取失败：本地 SDK / manual 核查

日期：2026-09-13。范围：文件、原始 SDK 安装包、手册与无网络模拟。没有启动 App、创建真实 Device、连接仪器、切换模式或写参数；没有修改生产代码。

## 结论

目前不能把五项失败认定为已修复，也不能据此认定磁体硬件故障。App 对四个接口的参数顺序、成功返回值拆包均符合随设备交付的 SDK。Default 的特殊参数顺序得到 Python、MATLAB、C# 源码和 C 声明交叉支持；这排除了“仅 App 将参数传反”的直接解释，但不能证明交付 SDK 与当前运行固件一致。

Current 的 count / 可读 index 不一致，与 Default 的 index 相关错误是两个需要厂家解释的现象；没有证据支持把两者用统一 index 偏移或参数交换修复。

新增发现：SDK 错误处理会额外调用只读 `com.attocube.system.errorNumberToString`。五项失败按当前 SDK 路径产生五次错误文字查询。这不是扫速表重试，亦非写入，但“底层 RPC 仅四类扫速表 getter”的既有说明不完整。

## 1. 实测证据边界

用户第一次截图中两张表 count 均为 5，channel=0。Current 成功 index 0..3，原始 `(range, rate)` 分别为 `(40, 0.0344)`、`(44.0116, 0.0172)`、`(45, 0.0172)`、`(46, 0.0086)`；index 4 返回 `VALUEOUTOFRANGETOOHIGH`，code 11。Default index 0 为 `(40, 0.0344)`，index 1/2 返回 `HARDWARENOTAVAILABLE`，code 9；index 3/4 返回 `VALUEOUTOFRANGE`，code 10。

用户 03:22:29 第二次日志再次出现相同五项错误，自动读取耗时 6.889 s；之前一轮 telemetry 为 9.187 s。第二次没有新表格截图，因此不声称成功行的数值再次得到确认。03:22:38/43 已出现磁体准备阶段提示，但没有准备最终结果。

第一次重复计数 10 已由 UI 聚合去重修复；第二次显示 5 证实汇总变化，不代表五个 SDK 错误消失。

## 2. App 和 SDK 调用链

仓库 `utils/config.py:292` 的默认 SDK 目录为 `D:/Insturment control v3/CRYO2100`；此次检查该目录和仓库副本。无法仅由日志证明运行中进程实际加载的模块文件，若用户曾覆盖配置仍须核对。

`app/devices/attodry2100_adapter.py` 的 `read_ramp_tables()` 直接取 count，再用 `range(reported)` 请求各行。Current 使用 `getter(channel,index)`，Default 使用 `getter(index,channel)`；不对原始 range/rate 换算。

| 接口 | SDK 参数 / JSON-RPC params | Python 成功返回 |
| --- | --- | --- |
| getNumRampRates | `[channel]` | `response[1]` |
| getRampRate | `[channel,index]` | `response[1], response[2]` |
| getNumDefaultRampRates | `[channel]` | `response[1]` |
| getDefaultRampRate | `[index,channel]` | `response[1], response[2]` |

命名空间：`com.attocube.cryostat.interface.magnet`。`ACS.Device.sendRequest()` 指定 JSON-RPC `2.0` 与 `api: 2`；该 API 字段不是设备固件版本号。

核对路径：本地 `CRYO2100/magnet.py` 的 Default 第 34/50 行、Default count 第 240/254 行、Current count 第 258/272 行、Current 第 372/388 行。

## 3. 原始发行包交叉核对

来源根目录：`D:/Dropbox/porject abstract/Setup testing/Attocube 2100 Setup/atoodry2100 disk/Software/API`。

- Python：`api_atto-device-python_1.0.12.whl` → `atto_device/CRYO2100/magnet.py`。
- MATLAB：`api_atto-device-matlab_1.0.12.zip` → `atto-device-matlab-1.12.0/atto_device/CRYO2100/magnet_getDefaultRampRate.m` 第 1/13 行明确 `(index,channel)` 并按此顺序生成 JSON。
- C#：`api_atto-device-csharp_1.0.12.zip` → `atto-device-csharp-1.12.0/atto_device/CRYO2100/generatedAPI.cs` 第 1248 行为 `Magnet_GetDefaultRampRate(int index, int channel)`，下方调用按同顺序传递。
- C：`api_atto-device-c_1.0.12.zip` → `atto-device-c-1.12.0/atto_device/CRYO2100/CRYO2100-win-lib-64/attoDry-1.0.0.h` 第 1241 行声明 Default `(deviceHandle,index,channel,...)`，第 1582 行声明 Current `(deviceHandle,channel,index,...)`。这里只证明 C 声明，未执行 DLL。
- LabVIEW zip 中也有 `magnet/getDefaultRampRate.vi`，未用 LabVIEW 打开或执行，不能从文件名推断连线顺序。

实际 SDK 目录与 wheel 的 `magnet.py`、`ACS.py`、`system_service.py` 在统一 CRLF/LF 后内容一致；仓库 magnet 副本也一致。wheel 中 magnet.py 原始 SHA-256：`57692dc930e391bee3325f40ae2e344d1f0e8eb59105144562c2c1bdb70e6058`。

包外名写 `1.0.12`，内部 `METADATA` 是 `Name: atto-device`、`Version: 1.12.0`；其他语言归档根目录也是 `1.12.0`。磁盘另附 `FW_1.1.0_colibri-imx7-emmc_cryo-image-cryo-colibri_update.enc.img`。这些文件名均不能证明实机安装版本，也未尝试解密或刷固件。

这些 wrapper 的 index、range、rate 文档字段没有填写单位或合法边界；count 只描述数量，没有解释是否包含隐式末段或 Fast。同一套生成式 wrapper 相互一致，不等于多份独立固件实现证据。

## 4. 错误文字查询和无网络验证

`CRYO2100/ACS.py:153` 的 `handleError()` 读取 `response[0]`。非零时先调用 `system_service.errorNumberToString(language,errNo)` 再抛出异常。`system_service.py:78` 明确此函数是错误码说明查询；language=0 返回错误名称，language=1 为较友好文字。本地默认 language=0，错误名字不是 App 自行解释出的硬件诊断。

本次离线验证只加载 Magnet/System_service 类定义，从 AST 提取实际 SDK `handleError` 和异常类，以记录请求的 fake 替代 Device。向 fake 提供与用户日志一致的五个错误码；没有 socket、没有真实 Device 实例、没有设备方法被执行。

开启断言的运行结果：PASS，12 次扫速表请求（2 count + 10 row）、5 次 `errorNumberToString`、5 个异常。Default 请求 params 顺序为 `[0,0]`、`[1,0]`、`[2,0]`、`[3,0]`、`[4,0]`。这验证当前 SDK 请求构造与错误路径；不验证真实固件如何解释这些参数。17 次是基于此 SDK 路径的推导和模拟结果，不是实机抓包。

错误文字查询同步发生在原 getter 调用内，因而仍在同一 owner 线程与现有连接内；没有隐式新连接或写入。它可能贡献耗时，但没有每次底层 RPC 的实测计时，不能将 6.889 s 中某一比例归因于此。

## 5. 手册可确认与不能确认的内容

- `Manuals & Specifications/01_220507_System-Spec-Sheet.pdf` 第 5 页：本系统 220507，电源 APS100。第 8 页：磁体系数 2044.9 G/A，0–40 A 对应 0.0344 A/s，40–44.0116 A 对应 0.0172 A/s。与前两行返回值数字一致。表中原始 45/46 不能据此视为本磁体允许的电流范围。
- `04_APS100 magnet power supply manual v1d1.pdf` 第 16 页：最多五段普通电流范围，另有 Fast。第 51 页：原生 `RANGE?` 范围选择 0..4，上限单位 A；第 52 页：原生 `RATE?` 选择 0..4 为五段普通速率，5 为 Fast，单位 A/s。
- 以上是 APS100 原生协议。attoDRY SDK 的组合 `(range,rate)` 到 APS 原生协议之间的服务器实现不在所查源码中，因此不能宣布 SDK index 5=Fast，亦不能用 T/min 标签。SDK 单位保持未确认，尽管前两行与 A/A/s 有强数值对应。
- `02_UM attoDry 2100 en_v4_2023-09-12.pdf` 第 32 页 Figure 18、78 页：触屏 General Information 可查看当前固件版本。第 61 页 Figure 50：Web About 页面提供 General Information 与 Product Files。用户可提供匹配当前设备的资料；不要求更新固件、reset 或 reboot。

已提取核对相关文本并查看规格第 8 页、APS 第 51/52 页、attoDRY 第 32 页渲染图。上述手册没有找到这四个 RPC 的固件兼容表或当前错误组合的解释。公开检索也未找到四接口的官方更正说明；[厂商产品页](https://www.attocube.com/en/products/cryostats/closed-cycle-cryostats/attodry2100)只是 LAN API 的概述，不能据此补全签名。

## 6. 保留假设，避免猜测修复

Default 在 index=0 成功、1/2 缺硬件、3/4 越界，**与服务器将第一个参数解释为 channel 的假设相容**；但缺少实际固件 schema 和实现，不能据此证明应该交换参数。所有所查发行 wrapper 均支持当前顺序。

Current 在 index=0..3 成功，故简单改成 1-based 会丢失已证实有效的第 0 行，且无依据说明能解决 index 4 失败。第五段是否有隐式边界、count 是否为能力值、或固件存在 off-by-one，均缺少证据；不选择其中任一假设写补丁。

## 7. 下一步所需证据 / 厂家问题清单

先由用户提供触屏 General Information 中的固件版本及设备型号/序列号；若 APS100 软件版本已知，一并记录。无需重新执行扫速表或改变任何设置。若现有 Product Files 中有该固件对应 API 包/schema，也可用于本地比对。

向厂家提供以上版本、系统 220507、四接口签名和两次错误记录，并询问：

1. 当前固件 `getDefaultRampRate` 的 JSON params 究竟是 `[index,channel]` 还是 `[channel,index]`？随设备发行的 1.12.0 wrapper 是否适配，是否存在已知更正？
2. 两个 count getter 返回 5 时，每张表合法 index 是哪些？是否包含隐式最终段、预留项或 Fast？为何 Current index 4 失败？
3. `range/rate` 的单位、区间边界定义及 SDK 到 APS100 普通段/Fast 的明确映射是什么？
4. `HARDWARENOTAVAILABLE` 在本接口指磁体 channel、默认表能力还是其他模块？单磁体系统的 Default 表是否完整支持？

未向厂家自动发送消息。证据不足时交付部分结果和明确错误，不交换参数、不探测额外 index、不改写 SDK、不开放扫速写入。本次核查结束，五项设备读取失败仍标为未解决。

## 8. 核查期间补充：准备超时及进度建议

用户随后提供同次操作日志：03:22:43 模式准备阶段，03:24:43 `Moving to Start: 0 → -2 T`，03:29:41 清理 Stop 完成、FAILED、0 spectra、`magnet did not reach Start before timeout`。

这明确说明准备流程因到位等待超时失败，不能将此次失败归于扫速表错误。进入 Moving 阶段说明模式准备调用已返回；Moving 日志在提交目标设置之前发出，本身不证明磁场实际开始移动。缺少最后一条 fresh field、读取时间、到位计数，因此不能断言磁体停滞或已接近目标。

代码证据：`MagnetPreparation.prepare()` 在模式准备返回后设置 `deadline=clock()+self.timeout_s`，随后才 fresh preflight、setpoint 和按需启动 field control；之后每轮 fresh snapshot 到位时累计稳定计数，要求 5 次。独立 Prepare 和 MCD Start 均传入 `cfg.attodry2100.mode_prepare_timeout_s`，默认 300 s。因此模式和移动的计时起点分开，但共用同一个配置数值；模式等待的两分钟没有从移动的五分钟预算中扣除。移动预算却包含其前置状态读取、命令和到位确认时间。

条件估算（不是实机测速，不使用未核实的 SDK 单位）：若本系统按规格第一段 0.0344 A/s、2044.9 G/A 充磁，则 `0.0344 × 2044.9 / 10000 = 0.007034456 T/s`，2 T 理想移动约 284.315 s；300 s 只余 15.685 s。该预算没有足够的明确余量，正常充磁加慢 RPC / 5 次确认也可能触发超时。具体此次失败原因仍需末次磁场证据。

建议后续实现（本次尚未修改代码）：

1. 进度页复用准备流程已有快照，显示阶段、当前/目标磁场、差值、数据年龄、已用/剩余预算、到位确认次数；模式阶段用已有模式准备快照显示实际状态，无法确认时明确 unknown。
2. 只有新快照到达时更新设备值；UI 本地计时可自行更新。日志按状态变化及节流周期（例如 15 s）记录，不为日志额外查询 SDK，不重新开启后台 telemetry，不制造高频无变化日志。
3. 模式准备与磁场到位使用独立、可配置的时间预算。到位预算覆盖命令完成、移动和确认；在 SDK 单位与映射未确认前，不从扫速缓存自动推导控制参数。不要通过改变物理扫速来掩盖等待时间问题。
4. 超时报告在清理前记录末次 field/时间/目标误差/确认次数/预算和错误；清理 Stop 单独记录结果，不将清理后的读数混作超时前读数。

必要验收是虚拟时钟+模拟慢充磁/慢读取：模式与移动预算独立、正常约 284 s 移动加确认可完成、真实超时仍失败并保留清理、进度功能不增加 SDK 调用数量。实机由用户操作。

## 9. 用户补充的厂商 Ramp rates 界面截图

来源：`C:/Users/commo/AppData/Local/Temp/codex-clipboard-f99e7087-228d-4188-9a7a-8c21893e6c65.png`，用户提供，未由 agent 操作界面。

| 厂商界面行 | 下界 A | 上界 A | Ramp rate A/s | 与首次 Current 读表关系 |
| --- | ---: | ---: | ---: | --- |
| Magnet Z zone 1 | 0 | 40 | 0.0344 | SDK index 0 的数值一致 |
| Magnet Z zone 2 | 40 | 44.0116 | 0.0172 | SDK index 1 的数值一致 |
| Magnet Z zone 3 | 44.0116 | 45 | 0.0172 | SDK index 2 的数值一致 |
| Magnet Z zone 4 | 45 | 46 | 0.0086 | SDK index 3 的数值一致 |
| Magnet Z zone 5 | 46 | 100 | 0.0043 | SDK index 4 先前读取失败，未读回确认 |

证据更新：厂商界面明确显示 A / A/s，前四段与 Current 原始 `(range,rate)` 顺序和值完全吻合。因此本系统这四行的 range 对应区间上界、rate 对应 A/s 已有直接界面交叉证据，不再只是规格数值相似。截图并非同一时刻的协议抓取；它不能独自确证运行中固件的接口契约、全部 index 的合法性或设置是否已应用。

界面存在 Apply / Discard 和 Max rates / Training rates / Constant rate 控件，但截图没有证明存在未保存修改，也没有证明已保存状态或活动预设。第五行是明确显示的 Zone 5，不能叫作 Fast；100 A 是界面条目值，不能当成本系统磁体允许达到的电流。

Current count=5 与界面五段数量一致，故不能为消除报错而把 count 强行改成4。第五行在界面存在但组合 getter 失败，需进一步核对厂商 UI 的读数来源/该固件 API 定义，区分接口边界问题、不同读取流程或界面待应用数据。Default 参数顺序和错误原因仍未解决；截图没有固件版本。

下一证据仍为 General Information 的固件版本，并由用户说明这张图是否在未编辑状态下打开、设置是否有待应用修改。不要仅为核查点击 Apply、Max rates、Training rates、Constant rate 或 Restart Magnet Control。若后续取得用户提供的厂商页面已有读取响应/匹配 API 文件，可以离线分析；不自动访问仪器或抓取真实设备通信。

## 10. 当前固件版本已由用户截图确认

来源：`C:/Users/commo/AppData/Local/Temp/codex-clipboard-44f00cbe-3359-4b4b-904d-7e5d8f127439.png`。

- Device type：attoDRY2100。
- Device name：2100e。
- Firmware version：**1.1.5**。
- 电子控制器序列号：**eNSP 23 S1 0105**（与系统规格书的整机系统号 220507 分别记录，不混用）。

此前索要固件版本的证据缺口已关闭。本地 1.1.0 固件文件不是实机版本；Python 包内部 1.12.0 是另一版本序列，不能因与固件 1.1.5 字符不同就断言不兼容。需厂家兼容说明或运行中 1.1.5 对应的 API 定义才能定论。

本轮本地文件名检索与公开官方资料检索未找到 1.1.5 的扫速接口兼容说明或更正。没有据此升级/降级固件或改参数。下一步证据应是用户提供的该设备 About / Product Files 内可用 API 包（若列出），或用户保存的厂商 Ramp rates 页已有请求/响应及页面源码，用于离线确认 UI 读取第五段所走接口；不得假定 Product Files 一定包含 API。仍无法确认时按第7节问题清单向厂家核实，附当前固件版本与控制器序列号。

## 11. SupportLog 检查结果

用户说明 Product Files 仅提供 SupportLog，并提供本地文件 `C:/Users/commo/Downloads/SupportLog`。

- 长度：1,069,009 bytes。
- SHA-256：`b0687455628920f09c6d03245c3cf5253fbddfe83bcfb40aa7d78168277d3d90`。
- 文件头为 OpenPGP：offset 0 是 Tag 1 公钥加密会话密钥包，body 长268，version 3，公钥算法1（RSA）；offset271 为 Tag18 加密且完整性保护数据包，body长1,068,732，version1。两包长度与文件总长吻合。
- 包类型依据 [OpenPGP RFC4880](https://www.rfc-editor.org/info/rfc4880/) 核对。不是可直接展开的明文日志/普通ZIP。没有对应解密私钥，未读取到内部日志，因此无法确认其中是否包含扫速表或接口错误。

没有尝试寻找用户其他私钥、没有解密、上传或发送该文件。文件来源提示它可能面向厂家支持使用，但包头不能证明密钥持有人身份。可由厂家确认能否解密并检查；App问题取证仍可用用户提供的厂商页面请求/响应。此文件不构成改变SDK参数或扫速预算来源的依据。
