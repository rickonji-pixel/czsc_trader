# S008 EX31 执行记录

状态：`EXECUTION_ERROR`。CPI 请求起点已成功对齐到月份首日，本次结构化诊断确认请求边界为
2011-11-01至2024-12-30，供应商规范化结果边界为2011-11-30至2024-12-31。起点满足边界，
但 CPI 代表日期按自然月末记录，最后一行比最后信号日多一天，DFLS 因而以“published dataframe
exceeds the requested time boundary”拒绝结果。

错误发生在完整准备、组件物化、BuyHold和Optuna之前。实验读取了部分开发池输入，没有计算目标
收益；三套原型均为0/1,500 trial，8逻辑核心搜索未启动。
