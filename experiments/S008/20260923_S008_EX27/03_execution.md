# S008 EX27 执行记录

状态：`EXECUTION_ERROR`。EX27 已通过补齐后的 SRT 候选加载，但在准备第一个交易日历输入时返回
`RuntimeExecutionError: data preparation failed for trading_calendar: TUSHARE_TOKEN is not configured`。
根因是 SRT 内部 DataRequest 没有显式传递仓库 `.env`，实验进程也没有在准备前把该受保护配置
加载到环境。

DFLS 未返回任何数据，未读取行情或收益，未计算 BuyHold，未创建 Optuna study；三套原型均为
0/1,500 trial。
