# 模块边界待办

本文记录当前代码中仍存在的模块边界债务。已经收口的事项保留简要结论，避免后续重复设计；
新增修复必须先确认业务边界，再进入独立实现任务。

## 已完成：DFLS 与主调方

- DFLS在门面内部执行统一校验、按“供应商＋标的”补丁修复和重新校验；主调方只处理
  `DataResult`。
- 每个补丁使用独立源文件并精确注册供应商与标的，未知异常继续失败阻断。

## 已完成：SRT 与 PTE

- SRT通过`load_strategy_runtime_context(...)`认证generation、publication、文件哈希、内容身份
  和策略契约；PTE消费返回的`StrategyRuntimeContext`。
- PTE保留活跃账户覆盖、跨标的截止日同步、账户事实、渠道状态和调度判断，不解析SRT发布文件。
- SRT拥有目标仓位、参考价、委托参数和生效时点；PTE提供账户事实并执行SRT计划。
- 账户图表和生产预检均通过SRT公共读取能力获取策略数据，不独立解释发布契约。

## 待处理：TDR 与 SRT

- `src/czsc_trader/generation_integrity.py`解释SRT generation的文件结构和哈希。该能力应迁入
  SRT，由TDR只消费认证结果。
- `src/czsc_trader/application/data_service.py`在generation提交后组合TDR文件校验、SRT
  publication读取和运行时校验。需要区分TDR数据池事务与SRT发布物鉴真职责。
- `src/czsc_trader/backtesting/datasets.py`仍直接加载行情、执行价格和执行清单；
  `src/czsc_trader/backtesting/srt_bridge.py`同时接收这些TDR组装的`ReplayData`和SRT运行上下文。
  普通回测应由SRT闭环发布并返回已认证输入，TDR只指定并校验回测评价窗口。

## 待处理：双轨发布

SRT生产发布目前同时写入按release保存的标准publication和历史兼容的平面行情文件。PTE已通过
统一SRT入口消费它们，不再自行对齐；仍需评审平面兼容文件是否存在其他有效主调方，再决定是否
删除双轨文件和相关写入逻辑。
