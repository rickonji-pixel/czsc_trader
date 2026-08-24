# 研究设计

## 数据边界

- 重排输入只允许使用Git跟踪的EX08 `trial_ranking.csv`和`trials.csv`。
- 排名前排除Trial 0；Trial 0只作为EX04等价性控制项。
- 候选列表、完整参数与来源哈希落盘冻结后，才允许读取2026。
- 2026只加载一次，所有冻结候选复用同一份行情和候选因子矩阵。

## 三套规则

所有排序均为降序，规则内后续字段只用于打破并列：

1. 稳健优先：`min_return_delta`、`win_count`、`median_return_delta`、`mean_return_delta`；Top 3为3123、2806、629。
2. 胜出数优先：`win_count`、`mean_return_delta`、`median_return_delta`、`min_return_delta`；Top 3为2385、2806、2478。
3. 平均收益优先：`mean_return_delta`、`median_return_delta`、`min_return_delta`；Top 3为3384、1192、2214。

Trial 2806重复入选，因此冻结8个唯一候选：629、1192、2214、2385、2478、2806、3123、3384。

## 2026报告

- 每个候选逐窗口报告EX04收益、候选收益、收益增量、双方夏普率和PASS。
- 额外按2026胜出窗口数、平均收益增量、最差收益增量生成报告排名；该排名不触发调参。
- 每个候选独立生成订单与因果审计，任何审计失败都使该候选FAIL。
- 无论整体PASS、FAIL或ERROR都生成完整实验档案。
