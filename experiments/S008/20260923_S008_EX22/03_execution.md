# S008 EX22 执行记录

补充`exit.limit_ratio`后，TXE已越过空仓和退出计划构造；第一套原型出现入场目标时，因执行合同
缺少必需字段`entry.limit_parameter`抛出`KeyError`并失败关闭。本轮只使用合成数据，没有读取
真实市场收益、封存池或启动参数搜索。

