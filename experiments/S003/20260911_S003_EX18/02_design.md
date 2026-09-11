# S003 EX18 设计

## 固定机制与执行

- `PRIOR_INTRADAY_CONTINUATION`：前一日日内方向延续，次日开盘执行，11:30退出；
- `LATE_SESSION_FLOW_CONTINUATION`：前一日尾盘方向延续，次日开盘执行，11:30退出；
- `OPENING_GAP_REVERSION`：开盘缺口方向反转，09:40执行，11:30退出；
- 50%昨日可卖底仓＋50%现金滚动，满足T+1；
- 基准单边成本1.2bp，压力单边成本6bp。

每个机制使用EX17完全相同的事件日期，同时计算：

1. `PRIMARY`：预注册方向；
2. `OPPOSITE`：完全相反方向；
3. `FIXED_LONG`：相同日期固定增加多头，用于识别日期选择能力。

EX16的`OPEN_TO_1130`或`SIGNAL_0935_TO_1130`每日固定多头结果作为对应时段的无条件对手。

## 裁决

所有可行变体必须同时满足：压力成本后平均收益大于0、盈亏比大于1、T+1账户终值相对静态
底仓为正、2021—2026至少4个年度平均收益为正。

- `PRIMARY`还必须同时胜过`OPPOSITE`和`FIXED_LONG`，才标记`FEASIBLE_SIGNED`；
- `FIXED_LONG`还必须在基准成本后胜过EX16对应时段的每日固定多头均值，才标记
  `FEASIBLE_TIMING`；
- 其余主变体标记`DIRECTION_FAIL`或`TIMING_FAIL`，对照保持`CONTROL`。

合格变体只获得统计与结构审计资格。结果产生后不增加退出时点、regime过滤、事件门槛或变体。
