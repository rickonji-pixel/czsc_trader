# EX08 纯内存4096 Trial正式研究设计

## 1. 研究身份

实验编号固定为 `0824_EX08`。本轮是独立正式研究，不是EX07续跑、覆盖或复现。EX07仍以Git跟踪档案中的608个Trial和FAIL结论为准。

本轮单一假设是：EX07受SQLite逐参数和逐属性事务限制，只完成608个Trial；在候选、参数化、采样器、目标和交易执行全部冻结的情况下，仅将运行态存储替换为纯内存并固定完成4096个Trial，更充分的搜索可能找到在收益率稳健性上严格优于EX04的挑战者。

## 2. 研究基线与PASS

公平研究基线继续使用：

```text
experiments/0824_EX04/artifacts/frozen_challenger.json
SHA-256: c6fa86c0f87743564231dec4fb6e66971bde0fa4107829d077cb5cf31c53885f
```

旧冠军 `baseline_20260823`、EX06和Buy & Hold只作报告参照，不参与选优或PASS。

冻结挑战者必须在以下三个2026窗口的收益率全部严格高于EX04才算PASS：

- `2026Q1`；
- `2026H1`；
- `2026M1-M8`。

夏普率、最大回撤、持仓率和交易次数完整报告，但不参与搜索、排名或PASS。

## 3. 冻结不变量

EX08逐项沿用EX07：

- EX06的91项候选因子及顺序；
- 94维Trial参数化：`active_factor_count`、91个原始权重、`enter_threshold`、`exit_gap`；
- 非零因子数量6—18；
- 日线原始成交量因子强制入选；
- 至少一个EX04原始趋势因子入选；
- 单项非零绝对权重不低于0.0125；
- 原始趋势权重合计不低于0.10；
- 权重L1范数为1；
- 四层计分公式、入场/离场状态机和最短持仓3日；
- 当日收盘后形成目标仓位，下一交易日开盘执行；
- 只做多或空仓、单边0.05%费用和独立现金账本复核；
- 2022H1至2025H2八个验证窗口；
- 目标为最大化八窗口相对EX04的最差收益增量；
- 完整排名依次使用最差收益增量、胜出窗口数、中位数增量、平均增量、较少非零因子、较小Trial编号；
- Trial持仓摘要、订单因子溯源和无前视审计规则。

候选和来源哈希固定为：

```text
EX06 protocol:           e47b31a21d5b309553ec2819878cabaabb0b7f55e54a5a03e0def9c25666649f
factor_candidates.csv:   a27eec780e9b49951200467468853ebdc0ac160de1fda6b6c9a50e4e0625e4eb
factor_universe.json:    7ac73a46db6210f9b2b79d16780a9985cdd3a509b229217d71e10f7cdb408f9f
EX06 frozen challenger:  868e989439e4442b4637f7976f438705e61b1d023f688ab7ce9d2ee1d0ac7b89
candidate names:         19d0c0d9eabf9147f1f84c68ba8a24eb061b4b93db12d22a0e1b1e86567252b9
```

任一哈希、候选名称或顺序不符时，本轮立即ERROR，不允许搜索或访问2026。

## 4. 样本边界

策略选择只能加载截至 `2025-12-31` 的受跟踪数据分区。数据加载审计必须证明冻结前没有打开任何文件名含 `_2026` 的CSV。

2026约束按本轮实验执行：

1. 4096个Trial全部完成前不得读取2026；
2. 第一名Trial必须无缓存重建并通过参数、持仓、八窗口指标和因果复核；
3. `frozen_challenger.json`写入并计算SHA-256后才允许加载2026；
4. 三个既定窗口只验收一次；
5. 2026结果不得反馈给参数、因子、排名、停止点或第二次搜索。

## 5. Optuna协议

固定使用：

```text
Optuna:          4.9.0
Sampler:         TPESampler
seed:            20260824
n_startup_trials: 256
multivariate:    true
group:           false
constant_liar:   true
batch_size:      8
n_jobs:          8
backend:         loky
inner threads:   1
completed Trials: 4096
```

EX04完整策略继续入队为Trial 0，且其目标值必须精确为0。

停止条件只有：

- 4096个Trial全部完成；或
- 协议、哈希、数据边界、任务完整性、参数投影或因果审计错误导致ERROR。

不设置墙钟上限，不使用停滞提前停止，不因当前最佳值提前停止。

## 6. 纯内存执行

Study使用Optuna `InMemoryStorage`。EX08不得创建、读取或恢复SQLite数据库，也不得把两个独立进程的Trial拼接成一次正式运行。

纯内存不提供中断恢复：

- 若进程在冻结前异常退出，本次尝试作废；
- 执行记录必须保留异常时间、已完成数量和原因；
- 未访问2026时可以按相同已提交协议从Trial 0重新开始完整4096 Trial；
- 新运行不得复用失败运行的采样历史或部分排名；
- 若异常发生在2026解封后，不得重跑搜索或二次验收，必须归档ERROR和已有证据。

正式搜索期间每批记录参数生成、并行评估、结果写回和总耗时。性能数据只解释执行效率，不参与策略选择。

## 7. 软件结构

新增EX08专用正式运行模块和脚本，保持三个边界：

1. **选择输入**：只读加载2020—2025、验证哈希并构造EX07同源候选；
2. **纯内存搜索与冻结**：固定4096 Trial，导出全部Trial和排名，复核第一名并写入冻结文件；
3. **独立留出**：只接受已落盘且有SHA-256的冻结文件，随后加载2026并生成验收、订单和审计。

现有EX07正式入口继续显式使用SQLite，历史行为不变。工程性能基准入口继续只写 `outputs/benchmarks/`，不得充当EX08正式入口。

## 8. 预注册与Git顺序

正式运行必须遵循：

1. 在 `codex/0824-ex08-inmemory-search` 分支创建 `experiments/0824_EX08/`；
2. 写入 `01_goal.md`、`02_design.md`、预注册 `artifacts/protocol.json` 和实施计划；
3. 运行协议/边界聚焦测试；
4. 提交预注册文件和运行代码；
5. 确认工作区状态并记录执行提交SHA；
6. 启动正式4096 Trial；
7. 搜索完成、第一名复核并冻结后才访问2026；
8. 写入 `03_execution.md`、`04_conclusion.md`、全部正式产物和清单；
9. 完成测试、编译、依赖和档案验证；
10. 提交PASS、FAIL或ERROR结果。

正式运行前提交的目标、候选、排序、停止条件和PASS定义不得在看到运行结果后修改。

## 9. 正式产物

EX08至少跟踪：

```text
experiments/0824_EX08/01_goal.md
experiments/0824_EX08/02_design.md
experiments/0824_EX08/03_execution.md
experiments/0824_EX08/04_conclusion.md
experiments/0824_EX08/implementation_plan.md
experiments/0824_EX08/experiment_manifest.json
experiments/0824_EX08/artifacts/protocol.json
experiments/0824_EX08/artifacts/candidate_identity.json
experiments/0824_EX08/artifacts/event_signal_support.json
experiments/0824_EX08/artifacts/factor_candidates.csv
experiments/0824_EX08/artifacts/factor_universe.json
experiments/0824_EX08/artifacts/trials.csv
experiments/0824_EX08/artifacts/trial_ranking.csv
experiments/0824_EX08/artifacts/study_summary.json
experiments/0824_EX08/artifacts/batch_timings.csv
experiments/0824_EX08/artifacts/factor_weights.csv
experiments/0824_EX08/artifacts/frozen_challenger.json
experiments/0824_EX08/artifacts/holdout_metrics.json
experiments/0824_EX08/artifacts/orders.csv
experiments/0824_EX08/artifacts/causal_audit.json
```

`trials.csv`必须包含4096个完成Trial且失败数为0，除非实验按ERROR归档。`study_summary.json`必须记录存储模式、完成数量、停止原因、总耗时、分阶段耗时、最佳Trial、冻结哈希、冻结前/后2026访问状态、依赖版本、执行提交和数据哈希。

## 10. 测试与工程验收

实现采用TDD，至少证明：

- EX08协议只接受纯内存、固定4096、无墙钟和无停滞停止；
- EX08入口不会创建SQLite或读取EX07 runtime；
- EX07正式入口仍显式使用SQLite；
- Trial 0精确复现EX04；
- 搜索只打开不晚于2025-12-31的数据；
- 未完成4096 Trial时不得冻结；
- 冻结文件不存在或哈希不符时不得加载2026；
- 搜索失败不会生成伪冻结挑战者；
- 4096 Trial导出、排名和批次计时数量完整；
- PASS只比较三个窗口收益率且要求全部严格高于EX04；
- 无论PASS、FAIL或ERROR，档案清单可验证。

正式运行前执行聚焦测试。归档后执行完整 `pytest -q`、`compileall`、`pip check`、`git diff --check` 和全部实验档案验证。

## 11. 明确不做

- 不改变因子、候选、参数范围或权重投影；
- 不改变TPE配置或随机种子；
- 不压缩94维参数；
- 不删除Trial属性或回测指标；
- 不依据EX07或EX08的2026表现调整本轮；
- 不覆盖EX04、EX06或EX07文件；
- 不把工程benchmark文件当作正式研究证据；
- 不使用Git worktree；
- 不在没有明确成交回报时推断任何实盘订单已成交。
