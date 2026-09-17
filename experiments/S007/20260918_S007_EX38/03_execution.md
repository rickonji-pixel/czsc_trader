# S007 EX38 执行

状态：`COMPLETE`。

- SRT 将冻结规则中的 `virtual_fill.sell=marketable_limit_at_open` 解析为渠道无关的 MARKET 卖单；显式限价退出仍保持 LIMIT。
- S007-v1 发布新增哈希固定的冻结因子证据输入。研究截止日前读取冻结证据，截止日后拼接 DFLS 新数据，避免微小浮点重算差异改变历史目标序列。
- TDR 使用 SRT Runner 与 BacktestChannel 完成 2021-01-05—2026-09-02 全窗口回放。
- SE 独立检查订单与成交因果、账户账本、交易配对和指标，结果为 PASS。
- EX31、EX34、EX35、EX37 档案完整性均重新验证通过。
