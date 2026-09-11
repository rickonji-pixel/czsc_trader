# 研究设计

## 1. 单一研究假设

EX04相对历史冠军的落后可能由入场逻辑或退出逻辑主导。若同一机制在三个已确认落后窗口中贡献大部分负向财富差，且对应因果混合状态机能在至少两个窗口修复材料性收益，则该机制才有资格成为下一轮优化对象；否则结论必须是混合或证据不足。

## 2. 数据与窗口

- 仅加载2020—2025的588080后复权30分钟、日线和周线行情；
- 2020只用于因子预热；
- 正式窗口固定为2021H1—2025H2十个半年，每个窗口独立以现金和零仓位开始；
- 主要落后窗口固定为2021H1、2023H1、2025H2，不根据本轮结果改变；
- 其他七个半年是对照窗口；
- 费用、次日开盘执行、只做多/空仓及状态机与EX04保持一致；
- 不打开文件名含`2026`的行情文件，不调用留出入口。

## 3. 固定策略与身份

通用历史冠军从注册表解析，并重新证明其12因子四层摊平策略与原规则得分、仓位完全一致，最大得分绝对误差不超过`1e-12`。EX04因子、权重和阈值只从受跟踪冻结文件读取；两者因子名称和顺序必须完全一致。

## 4. 精确路径账本

每个半年窗口分别回测通用基线和EX04，逐交易日保存：

- 双方决策仓位与次日实际执行仓位；
- 双方权益日收益；
- `log_wealth_delta = log1p(EX04日收益) - log1p(基线日收益)`；
- 仓位组合：`both_cash、both_long、baseline_only、ex04_only`；
- T-1可知的60日市场状态；
- 基线持仓区间和路径机制标签。

每个窗口的`log_wealth_delta`之和必须严格等于：

```text
log(1 + EX04窗口收益) - log(1 + 基线窗口收益)
```

允许绝对误差不超过`1e-12`。正式归因份额使用对数财富差，普通日收益差只作阅读辅助。

## 5. 市场状态

沿用0825_EX02标签，交易日T只使用不晚于T-1收盘的信息：

- 前一交易日相对60个交易日前涨幅不低于10%：`uptrend`；
- 不高于-10%：`downtrend`；
- 其余：`sideways`；
- 不足60日：`warmup`。

输出`半年窗口 × 市场状态 × 仓位组合`的日数、暴露、市场收益、双方收益和财富差。该标签不得产生或覆盖仓位。

## 6. 基线持仓区间与机会损失机制

在每个窗口内，以通用基线实际执行仓位的连续持有段作为基线持仓区间。对区间内`baseline_only`交易日按EX04覆盖位置分类：

- `fully_missed_entry`：整个基线持仓区间没有任何EX04持仓重叠；
- `late_entry`：位于该区间首次EX04持仓之前；
- `early_exit`：位于该区间最后一次EX04持仓之后；
- `interrupted_holding`：位于两段EX04持仓之间；
- `unclassified_baseline_only`：不满足以上定义时保留并触发审计警告。

非`baseline_only`交易日分别标记`both_cash、both_long、ex04_only`。每个机制报告全部财富差、负向财富差、正向财富差、交易日数和区间数。

“机会损失”只指`baseline_only`日中负向的`log_wealth_delta`，不能把所有落后窗口或全部上涨日统称为机会损失。

## 7. 决策事件得分归因

保存两类重点信号日：

1. 基线从空仓转为持仓而EX04仍空仓：`missed_baseline_entry`；
2. EX04从持仓转为空仓而基线继续持仓：`early_ex04_exit`。

每个事件记录：

- 双方总得分、入场/退出阈值及阈值距离；
- 结构、趋势、量价位置三组的双方加权贡献；
- EX04权重配基线阈值、基线权重配EX04阈值是否满足当日对应条件；
- 所属窗口、市场状态、基线持仓区间和后续路径机制。

对漏入场事件的阻断标签：

- `weight_block`：只用EX04权重即不能通过基线入场阈值，而提高阈值单独不阻断；
- `threshold_block`：提高入场阈值单独阻断，而EX04权重单独不阻断；
- `independent_double_block`：两项单独都阻断；
- `joint_margin_block`：两项单独均不阻断但组合后阻断；
- `state_path_not_score`：当日得分条件不能解释仓位差异。

退出事件使用对称定义，区分权重、退出阈值、双重和路径状态影响。

## 8. 因果混合状态机

除基线、EX04、`weights_only`和`thresholds_only`外，运行两个混合反事实：

- `baseline_entry_ex04_exit`：空仓时使用基线得分和0.15入场；持仓时使用EX04得分和0.025退出；
- `ex04_entry_baseline_exit`：空仓时使用EX04得分和0.175入场；持仓时使用基线得分和0.00退出。

两个混合策略从各自仓位状态独立演化，沿用1日确认、最短持仓3日、1日退出确认和次日开盘执行；不得拼接已实现仓位序列或读取未来事件。

混合策略只作因果反事实，不是候选，不排名、不冻结、不访问2026。

## 9. 主因分类

在三个主要落后窗口中，先计算`baseline_only`负向对数财富差的机制份额：

- 入场族：`fully_missed_entry + late_entry`；
- 退出族：`early_exit + interrupted_holding`。

单一主因路径支持条件：某族合计至少占三个窗口池化机会损失的60%，且至少两个单独窗口份额不低于50%。

联合路径支持条件：入场族和退出族各自至少占池化机会损失的40%，且各自在至少两个单独窗口的份额不低于40%。该条件只用于联合分类，避免两个互斥份额不可能同时达到60%的逻辑死分支。

反事实支持条件：对应混合状态机相对EX04收益至少在两个主要窗口提高超过0.1个百分点，且三个主要窗口收益增量中位数为正。

分类规则：

- `entry_failure`：只有入场族同时满足路径和反事实支持；
- `exit_failure`：只有退出族同时满足两项支持；
- `joint_entry_exit_failure`：两族都满足联合路径支持和各自反事实支持；
- `mixed_path_failure`：均不满足，但池化机会损失可以完整归因；
- `insufficient_path_evidence`：存在未分类路径、归因不闭合或材料性样本不足。

权重/阈值事件标签只解释入场或退出族内部来源，不单独覆盖上述主分类。

## 10. 后续决策边界

- `entry_failure`：下一轮才可研究入场得分表达或入场阈值；
- `exit_failure`：下一轮才可研究退出确认或趋势持有；
- `joint_entry_exit_failure`：不得一次同时修改两层，先选择池化负贡献更大的机制；
- `mixed_path_failure`或证据不足：停止直接架构优化，不凭直觉增加状态交互；
- 若权重/阈值阻断标签跨三个窗口不一致，不得据此调权重或阈值。

## 11. 正式产物

至少保存：

- `artifacts/protocol.json`；
- `artifacts/identity_audit.json`；
- `artifacts/variant_window_metrics.csv`；
- `artifacts/daily_path_ledger.csv`；
- `artifacts/window_regime_path.csv`；
- `artifacts/baseline_episodes.csv`；
- `artifacts/mechanism_contributions.csv`；
- `artifacts/decision_events.csv`；
- `artifacts/group_score_contributions.csv`；
- `artifacts/hybrid_counterfactuals.csv`；
- `artifacts/mechanism_classification.json`；
- `artifacts/metrics.json`。

无论`COMPLETE`或`ERROR`均如实归档。不得生成冻结挑战者、2026指标或订单。
