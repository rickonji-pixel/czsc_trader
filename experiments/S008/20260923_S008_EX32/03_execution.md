# S008 EX32 执行记录

状态：`EXECUTION_ERROR`。完整月频请求边界已通过 DFLS，数据准备成功。组件物化时发现 CPI 历史
从2011-11开始，而策略交易会话从2013-07-29开始；在首个交易会话前已经发布的19个月度记录
均被 `searchsorted` 映射到2013-07-29，触发“causal CPI effective sessions are not unique”门禁。

错误发生在组件物化、EX16逐值等价、BuyHold和Optuna之前。没有计算目标收益；三套原型均为
0/1,500 trial，8逻辑核心搜索未启动。
