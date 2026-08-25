# 研究结论

## 判定边界

本轮是`0824_EX04`决策边界稳定性诊断，状态为 **COMPLETE**。
不判策略PASS/FAIL，不生成挑战者，不访问2026。

## 机器分类

- 分类：`no_stable_boundary`。
- 跨折众数规则：`action_family=continue_hold|block_label=joint_margin_block`。
- 同一规则入选折数：1/5。
- 留出匹配事件：5个。
- 留出正向年份：0/5。
- 年度精确符号检验p值：1.000000。
- 留出池化平均行动价值：-0.017513。
- 最大单一正收益事件份额：76.05%。

## 门槛审计

- `same_rule_stable`：FAIL。
- `all_years_supported`：FAIL。
- `minimum_oof_support`：FAIL。
- `all_oof_years_positive`：FAIL。
- `annual_sign_test`：FAIL。
- `positive_pooled_oof_mean`：FAIL。
- `not_single_event_dominated`：FAIL。

## 研究含义

没有找到同时满足跨折规则一致、五年留出方向、支持数、精确符号检验和事件集中度的稳定边界。
因此，在当前数据、现有事前信息以及EX04与通用基线所张成的动作空间内，
`0824_EX04`应保留为当前证据支持的588080最佳可用研究策略；不再继续基于这些历史样本做回溯修补。

本结论不宣称EX04是数学上的全局最优；未来新数据或真正独立的新信息可以另立研究。
