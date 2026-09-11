# 实施计划

权威逐步计划见`docs/superpowers/plans/2026-08-26-ex04-decision-boundary.md`。

本实验按以下冻结顺序执行：

1. 提交目标、设计、协议、测试和实现；
2. 运行聚焦回归，确认2026隔离、源档案身份、区间闭合和按年留一选择；
3. 记录实现提交；
4. 仅通过`scripts/run_experiment.py --experiment-dir experiments/0826_EX01`正式执行一次；
5. 验证档案并提交机器证据，无论结论是否支持稳定边界。

不使用worktree或子agent，不调用`scripts/run_holdout.py`。
