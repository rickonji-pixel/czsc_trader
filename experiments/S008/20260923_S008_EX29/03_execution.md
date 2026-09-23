# S008 EX29 执行记录

状态：`EXECUTION_ERROR`。USDCNH 的跨市场日历修正已通过其 DFLS 请求，但后续在
`cpi_monthly`输入上返回“published dataframe exceeds the requested time boundary”。根因是 SRT
按2012-01-02请求月度数据，而供应商按参考月月首日返回2012-01-01；运行时没有为月频输入声明
允许覆盖月初及月度发布间隔的陈旧窗口。

错误发生在完整准备、组件物化、BuyHold和Optuna之前。实验读取了部分开发池输入，没有计算目标
收益；三套原型均为0/1,500 trial。
