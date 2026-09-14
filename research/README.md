# 研究项目索引

这里保存各策略研究线的当前上下文，便于OPC团队跨机器、跨会话继续工作。通用研究流程、
数据污染规则和评估规范见[研究交接](../docs/RESEARCH_HANDOFF.md)；不可变事实直接读取
`experiments/<策略ID>/`，正式身份、版本和资格直接读取`strategies/`。用户确认的通用风险、
收益和仓位偏好见[OPC交易策略研究目标](RESEARCH_MANDATE.md)。

| 策略 | 名称 | 标的 | 当前阶段 | 当前交接 |
| --- | --- | --- | --- | --- |
| S001 | 综合基线策略 | 588080.SH | PAPER_READY / PTE观察 | [S001](S001/HANDOFF.md) |
| S002 | 中证500择时策略 | 510500.SH | PAPER_READY / PTE观察 | [S002](S002/HANDOFF.md) |
| S003 | 成分资金流宽度早盘延续 | 510500.SH | PAPER_READY / PTE观察 | [S003](S003/HANDOFF.md) |
| S004 | 尾盘流动性错位修复 | 588080.SH | RESEARCH_PAUSED / 候选保留作标杆 | [S004](S004/HANDOFF.md) |
| S005 | 588080中频增强策略（终止） | 588080.SH | TERMINATED_NO_CANDIDATE | [S005](S005/HANDOFF.md) |

## 前瞻监测方案

监测方案绑定冻结版本和独立PTE虚拟账户，不按策略编号合并：

| 冻结版本 | PTE账户 | 监测方案 |
| --- | --- | --- |
| S001-v1 | `s001-v1` | [S001-v1](S001/candidates/S001-v1_MONITORING.md) |
| S001-v2 | `s001-v2` | [S001-v2](S001/candidates/S001-v2_MONITORING.md) |
| S002-v1 | `s002-v1` | [S002-v1](S002/candidates/S002-C001_MONITORING.md) |
| S003-v1 | `s003-v1` | [S003-v1](S003/candidates/S003-C001_MONITORING.md) |

交接文件只维护研究问题、数据截止、权威证据、当前结论和下一步。实验过程与指标明细不在
这里复制，避免交接材料演变成历史流水账。
