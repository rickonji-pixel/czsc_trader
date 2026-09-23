# S008 EX36 执行记录

状态：`EXECUTION_ERROR`。DFLS准备、13组件 EX16 逐值等价、完整 SRT 与加速历史全列信号等价
全部通过。完整 TXE 账户锚点却从开发池首日即 ETF 上市日2013-07-29开始，账户快照需要前一日
参考收盘价但上市前不存在价格，因而以“backtest account snapshot has no reference close”停止。
加速账户按合同使用13个归一化组件首次全部有效日，两条账户锚点的评价起点不一致。

错误发生在账户权益等价、BuyHold和Optuna之前。没有计算目标收益；三套原型均为0/1,500 trial，
8逻辑核心搜索未启动。
