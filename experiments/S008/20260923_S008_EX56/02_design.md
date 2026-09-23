# S008 EX56 实验设计

## 实现边界

实现只使用`518880.SH`复权行情、未复权执行行情、`XAUUSD.FXCM`、`XAGUSD.FXCM`和SSE交易
日历。金银特征统一滞后一交易日，保证每个中国决策日使用的FXCM源日期严格更早。状态机完整
表达EX55冻结的入场确认、最短持有和退出后冷却语义。

## 合成验证

使用确定性合成行情驱动慢速金银比与快速金银相对收益穿越锚点阈值。机器检查：运行时闭包哈希、
候选身份、输入合同、相邻窗口增量范围、因果源日期、0/100%仓位、入退场与冷却状态、单边10bp
及30bp的TXE完整窗口决策和成交。合成行情只验证可表达性，不形成Alpha证据。

全部检查通过则裁决`PROCEED_TO_PRECIOUS_METAL_PREFERENCE_SEARCH_PREREGISTRATION`；任一检查
失败则裁决`STOP_PRECIOUS_METAL_PREFERENCE_IMPLEMENTATION_GATE`。
