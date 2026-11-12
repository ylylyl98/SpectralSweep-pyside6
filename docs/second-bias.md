# 第二路 Bias

第二路是一台独立的 Keithley SMU，角色名为 `Vbias2`。原来的 `Vbias` 和旧实验配置继续使用。

## 连接和简化界面

在 Instruments 的 SMU 区域展开 **Configure second bias**，为 `Vbias2` 选择独立的 GPIB 地址，并设置、应用其电流限制和电压量程。不能将同一台仪器分配给两个角色。

未配置第二路且实验没有使用它时，相关参数和表格列隐藏。配置后，实验页面显示 **Use second bias**；取消勾选不会关闭仪器输出，也不会发送第二路设压命令。载入使用第二路的实验时，即使设备未连接，也保留设置；运行前会报告缺少的通道。

## 模式

- **Independent**：第二路独立设置电压。Dual Gate 使用自己的 start/stop；2D Sweep 可将它选为扫描轴，也可保持固定。Motion/MCD 条件生成器支持单值，或列表/范围与门压条件的全部组合。
- **Follow Vbias + offset**：每个点按 `Vbias2 = Vbias + Offset` 计算。Offset 可为正、负或零；不需要第二套扫描范围。
- 不使用：第二路不参与该实验。旧配置默认如此。

使用中的 0 V 是实际的设压目标，不能与“不使用”混同。手动控制中的 **Link Vbias2 to Vbias** 也支持偏移跟随；此时第二路目标只读。单独归零主路时，第二路跟随到 Offset；**Zero all** 将所有已连接的通道分别归零。

## Doping–bias 二维图

1. 在 2D Sweep 选择 **Physical D/F**。
2. Outer 选择 **Doping**，Inner 选择 **Vbias**，分别设置扫描范围。
3. 设置固定 **E-field** 和非零 **Ratio r**。
4. 开启 **Use second bias**，选择 **Follow Vbias + offset**，输入 Offset。

每点使用软件现有换算：

```text
Vtg    = (Doping + E-field) / 2
Vbg    = (Doping - E-field) / (2r)
Vbias2 = Vbias + Offset
```

第二路不增加扫描维度。例如 21 个 Doping 点与 41 个 Vbias 点仍为 861 个点。蛇形扫描反向时使用相同关系。图中的 bias 轴是第一路电压；若两路接在样品两端，它们的压差保持固定。

运行前会校验派生目标与两台设备的已验证电压量程。2D Sweep 还检查第二路的 Safety Limits；越界不会被截断为另一个电压。两台仪器按小步交错设压，稳定后采集，不保证硬件同时触发。

实验记录包含第二路模式、目标和读回；旧数据中不存在的字段不会被解释为测得 0 V。自动回零只将本次使用的第二路纳入，手动 Zero all 是显式的全部通道操作。
