# 20260909_S002_EX03 执行

状态：FAILED。

执行在语义复核一致性校验处终止。冻结协议把`bar_trend_V240209::空头`定义为负向风险
过滤假设；按原始UTF-8证据精确查询后，该状态3、5、10日平均净收益均为正，年度方向
一致性只有0.5，未进入机械复核池。此前终端乱码输出中的`多头`被人工误读成`空头`。

程序按设计抛出`every selected hypothesis must belong to the frozen mechanical review universe`，
未生成实验结果、候选或任何外部状态变更。
