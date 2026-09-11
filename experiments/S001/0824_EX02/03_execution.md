# 执行过程

## 正式执行

- 状态：`COMPLETE`
- 执行时间：2026-08-24T14:29:58.884734+08:00
- 代码提交：`25200bdbd80d8d171c4c1dff3f5cf9cf1fa474bc`
- Python：3.12.10
- CZSC：1.0.1
- vectorbt：1.1.0
- pandas：3.0.5
- NumPy：2.5.2
- Plotly：6.9.0
- 可见行情文件数：18
- 实际回测目标仓位数：70
- 2026样本外数据访问：否
- 冻结挑战者：未生成
- 机器证据：`artifacts/`

## 执行异常与修复

第一次正式运行在完成因子、信号和权重归因后，于阈值敏感性协议解析处中止：运行器把 `threshold_sensitivity.combine_families=false` 元数据误当成候选数组迭代，触发 `TypeError: 'bool' object is not iterable`。该次运行状态按协议记录为 `ERROR`，没有形成或查看归因结论。

根因修复遵循测试驱动流程：先将真实协议中的 `combine_families` 加入回归测试并复现错误，再将阈值参数读取限定为预注册白名单 `entry / exit`。聚焦回归测试 `20 passed`，修复提交为 `25200bd`。第二次正式运行使用相同预注册协议完成，未改变材料性阈值、分类规则或候选范围。
