# S008 EX30 执行记录

状态：`EXECUTION_ERROR`。CPI 已按预注册改为 `LATEST_AVAILABLE` 且最大陈旧期为62个自然日，
但 SRT 推导出的请求起点为2012-01-02；Tushare 的 CPI 接口按月份查询并返回该月代表行
2012-01-01，DFLS 的自然日边界校验因此以“published dataframe exceeds the requested time
boundary”拒绝结果。

错误发生在完整准备、组件物化、BuyHold和Optuna之前。实验读取了部分开发池输入，没有计算目标
收益；三套原型均为0/1,500 trial，8逻辑核心搜索未启动。
