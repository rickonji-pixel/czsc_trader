# 实施计划

1. 提交与EX01完全同规则、增加`retry_of`身份的EX02预注册。
2. 用失败测试锁定日线`dt`列规范化边界并提交单点修复。
3. 验证协议除实验编号和重试来源外没有漂移。
4. 只读取截至2025的数据选择27项候选；无合格候选则FAIL并停止。
5. 合格候选冻结并哈希后才执行2026FULL及三个诊断窗口。
6. 如实归档结果，更新交接并执行全套验证。

详细设计与开发步骤见 `docs/superpowers/specs/2026-08-30-downside-risk-position-optimization-design.md` 和 `docs/superpowers/plans/2026-08-30-downside-risk-position-optimization.md`。
