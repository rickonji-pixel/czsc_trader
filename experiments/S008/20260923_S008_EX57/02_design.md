# S008 EX57 实验设计

EX57复用EX56已冻结的研究期运行时和执行脚本，唯一合同修正是把允许数据集中的交易日历从错误的
`market.trading_calendar`改为平台枚举`calendar.trading_sessions`。执行前必须验证EX56完整归档、
协议、脚本与运行时文件哈希。其余设计和禁止事项全部继承EX56。
