# 588080 CZSC策略研究：跨机新会话交接

> **最高优先级约束：始终按跨设备接力处理。**
>
> 不得假定新会话与上一会话位于同一台设备、同一绝对路径，或保留任何未提交文件。不得依赖历史 `outputs/`、本地SQLite数据库、虚拟环境和终端输出。研究事实只以Git跟踪的源码、配置、`data/raw/*_manifest.json`、`data/raw/*_validation.json` 和 `experiments/MMDD_EXX/` 为准。`outputs/`只用于普通回测输出，不是研究档案；需要结果时必须在当前设备重新运行，并使用命令实际返回的目录。

本文档记录截至 **2026-08-26 / 0826_EX01及基线晋升** 的可接力状态。新会话应先验证仓库当前状态，不能把本文中的分支或提交描述当成未经核验的实时事实。

## 1. 当前目标与两个基线

### 1.1 长期研究目标

- 标的固定为 `588080.SH`；
- 历史数据覆盖上市日至2026-08-24；
- 截至2026-08-24的数据已经被项目反复观察，全部属于已见历史证据；
- `0824_EX04`已冻结为当前588080正式基线，不再利用已见历史样本继续回溯修补；
- 新的独立证据从2026-08-26之后的前向行情开始；
- 后续若开展新研究，必须预注册独立信息和验证边界，不得把2026重新包装成留出样本。

此前“单次实验内冻结后一次2026验收”的规则仍是历史实验事实，但截至当前，2026结果已经跨多轮进入研究判断，不能再承担独立样本外角色。未来研究必须以2026-08-26之后的新行情或另行批准的真正独立信息作为新证据。

### 1.2 当前活动正式基线

588080普通回测默认加载：

```text
configs/rule_baselines/baseline_20260826.json
```

实际默认版本由`configs/rule_baselines/registry.json`的`latest`决定。`baseline_20260826`与`experiments/0824_EX04/artifacts/frozen_challenger.json`逐字节相同，是12因子四层策略，只允许用于`588080.SH`。独立前向验证起点为2026-08-26。

### 1.3 历史归档基线

原通用固定规则：

```text
configs/rule_baselines/baseline_20260823.json
```

`baseline_20260823`曾是通用回测正式基线，但其形成过程使用了2026数据，现已降为`archived`。它只允许通过显式版本号复现历史，不能作为默认回测、当前冠军或新的独立比较对象。历史实验继续保留当时对它的真实引用，不做事后改写。

| 名称 | 用途 | 当前身份 |
|---|---|---|
| `baseline_20260826` | 588080默认普通回测、后续前向比较 | 活动正式基线，来源为0824_EX04 |
| `baseline_20260823` | 显式历史复现 | 归档基线，不再默认使用 |
| `experiments/0824_EX04/.../frozen_challenger.json` | 新基线来源与历史实验研究对象 | 冻结源档案，禁止改写 |

## 2. 不可违反的回测边界

1. 行情只能来自 `data/raw` 中已准备并通过验证的30分钟、日线和周线CSV。
2. A股股票和ETF数据统一使用Tushare后复权价格，复权方式为 `hfq`，因子来源为 `fund_adj`。
3. 588080的30分钟数据仅对2024年七个已确认的Tushare异常交易日做硬编码成交量修正；日期记录在 `data/raw/588080_manifest.json`。
4. 因子日 `T` 只能读取不晚于 `T` 收盘的已完成数据，目标仓位最早在下一交易日开盘成交。
5. 日频只做多或空仓，目标仓位只能是0或1；持仓率不是仓位比例，而是回测窗口中处于满仓状态的交易日占比。
6. 独立回测窗口从现金和零持仓开始，但首日可执行上一交易日已形成且仍有效的目标仓位，此类订单标记为 `InitialEntry`。
7. 每笔订单必须包含 `factor_event_id`，并通过目标仓位、事件方向和紧邻下一交易日执行审计。
8. 禁止收益锁定、基准表现触发、季度强制持仓、特殊日期交易或其他非CZSC仓位覆盖。
9. 普通回测与研究必须分开：回测只执行冻结规则；研究才允许生成候选、排名和冻结挑战者。

## 3. 当前四层策略架构

当前研究策略统一表示为：

```text
CZSC因子（包含类别归一化） → 权重 → 加权计分 → 入场/离场阈值
```

原来的“映射层”没有作为独立可优化层存在。类别到 `-1/0/+1` 或状态指示值的转换属于因子定义，避免同一语义在“因子”和“映射”两处重复表达。

### 3.1 EX04研究基线

- 非零因子：12项；
- 因子来自30分钟、日线、周线；
- 权重L1范数为1；
- 入场阈值：0.175；
- 离场阈值：0.025；
- 当日因子分数产生目标仓位，下一交易日开盘执行。

完整因子名称、权重和阈值必须从 `experiments/0824_EX04/artifacts/frozen_challenger.json` 读取，不要从本文手工重建。

### 3.2 EX06—EX08候选空间

- 候选总数：91项；
- 包括连续/类别因子以及低频事件状态；
- 事件型因子不再因低覆盖率或低激活天数被误删；
- 日线原始成交量因子强制保留；
- 至少保留一个EX04原始趋势因子；
- 非零因子数量限制为6—18；
- 每项非零绝对权重不低于0.0125；
- 原始趋势权重合计不低于0.10。

候选身份以 `experiments/0824_EX06/artifacts/factor_candidates.csv` 及其协议哈希为准。

## 4. 0824_EX01—0826_EX01研究进度

每轮实验的完整事实均位于 `experiments/<实验编号>/`：

- `01_goal.md`：目标与PASS边界；
- `02_design.md`：预注册设计；
- `03_execution.md`：实际执行过程；
- `04_conclusion.md`：结论；
- `experiment_manifest.json`：受跟踪文件哈希和实验元数据；
- `artifacts/`：协议、候选、指标、订单、审计和冻结策略。

| 实验 | 性质 | 关键变化 | 结论 |
|---|---|---|---|
| EX01 | 冠军挑战 | 再入场冷却期与门控 | FAIL；没有冻结挑战者，未访问2026 |
| EX02 | 诊断归因 | 因子、状态、映射、权重、阈值和状态机反事实 | COMPLETE；不判PASS，不产生挑战者 |
| EX03 | 架构实验 | 固定冠军12因子，改为四层架构并优化权重/阈值 | FAIL；风险调整表现改善，但2026收益未胜出 |
| EX04 | 收益单目标 | 固定12因子，优化权重和阈值 | FAIL于历史冠军；其冻结策略被指定为后续研究基线 |
| EX05 | 因子扩展 | 状态展开，18个非零因子 | FAIL；只在2026H1收益超过EX04 |
| EX06 | 因子缺陷修复 | 事件感知候选、91因子、Joblib并行搜索 | FAIL；候选空间更合理，但搜索结果未超过EX05 |
| EX07 | 搜索算法实验 | 固定EX06的91候选，用Optuna联合搜索子集、权重和阈值 | FAIL；608个Trial中没有新Trial超过EX04 |
| EX08 | 搜索深度/执行效率实验 | EX07同一搜索空间与TPE配置，改为纯内存并固定完成4096个Trial | FAIL；最佳仍为Trial 0（EX04），没有新方案超过EX04 |
| 0825_EX01 | EX08输出补测 | 三套规则各取Top 3，共8个唯一Trial，冻结后批量执行一次2026验收 | FAIL；8项全部未能在三个窗口严格超过EX04，审计全部PASS |
| 0825_EX02 | EX04机制归因 | 权重/阈值分解、因子组联盟、32项局部扰动、路径/状态归因和区块bootstrap | COMPLETE；EX04是状态依赖的防守型局部平台，不是精确参数尖峰，仍不能排除样本偶然 |
| 0825_EX03 | EX04交易路径归因 | 三个落后半年窗口的精确财富账本、入退场机制、决策阻断与因果混合状态机 | COMPLETE；主分类`mixed_path_failure`，退出侧是更强线索但不能全局回退退出逻辑 |
| 0825_EX04 | EX04离场信号诊断 | 20个离场分歧事件的继续持有反事实、上下文区分、连续特征和路径集中度 | COMPLETE；`path_concentrated_exit_failure`，多数离场有效，少数错误离场主导损失，禁止直接优化 |
| 0825_EX05 | 主导错误离场病灶解剖 | 展开`0824_EX04`的12个原始CZSC信号在20个离场前`T-20…T`的因果轨迹 | COMPLETE；`shared_but_contaminated_anatomy`，53个跨时间共同签名全部污染保护性离场，现有表示不足 |
| 0825_EX06 | 新离场表示诊断 | 预注册28个连续结构/能量/量价描述符，顺序确认并做364种精确标签枚举 | COMPLETE；`insufficient_new_representation_evidence`，2个表面低污染签名未通过多重比较校正，禁止晋升 |
| 0826_EX01 | 决策边界稳定性诊断 | 44个分歧区间、49条简单规则、按年留一 | COMPLETE；`no_stable_boundary`，停止利用已见历史继续回溯修补 |

### 4.1 EX07最终证据

EX07运行2小时后由墙钟上限停止：

- Optuna 4.9.0，固定TPE随机种子；
- 完成608个Trial，失败0个；
- Trial 0注入EX04以验证等价性；
- 总榜第一仍为Trial 0，目标值为0；
- 最佳新方案Trial 468在八个半年度窗口中胜出5个，平均收益增量为正，但最差窗口收益增量为 -3.2309%，未满足maximin目标；
- 冻结后才读取2026；由于冻结冠军就是EX04，三个2026窗口均与EX04相同，严格胜出条件全部FAIL；
- 因果审计PASS。

权威文件：

```text
experiments/0824_EX07/03_execution.md
experiments/0824_EX07/04_conclusion.md
experiments/0824_EX07/artifacts/study_summary.json
experiments/0824_EX07/artifacts/trial_ranking.csv
experiments/0824_EX07/artifacts/holdout_metrics.json
experiments/0824_EX07/artifacts/causal_audit.json
```

`experiments/0824_EX07/runtime/optuna.db`及SQLite的WAL/SHM/JOURNAL文件只用于运行恢复，已被Git忽略。它们不是研究证据，也不应跨机复制或提交。

### 4.2 EX08最终证据

EX08在执行提交`e5341a2a684fdd779c6d58a55094e8abd83a2ede`上以纯内存完成正式搜索：

- 固定完成4096个Trial，失败0个，512个批次；没有墙钟或停滞提前停止；
- 搜索耗时1195.81秒，吞吐约12331 Trial/小时，是EX07正式SQLite运行的40.84倍；
- ask/project耗时963.46秒（80.66%），并行评估228.08秒（19.09%），tell/属性写回2.98秒（0.25%）；SQLite消除后，主要瓶颈是94维多变量TPE参数生成；
- Trial 0仍为总榜第一，目标值为0；4095个新Trial均未超过EX04；
- 最佳新方案为Trial 3123，在八个半年窗口中胜出6个，中位收益增量+1.7247%、平均收益增量+2.3754%，但最差窗口收益增量-0.2356%，未通过maximin目标；
- 冻结结果仍是EX04的12因子、权重和阈值；冻结SHA-256为`724d231a6b5ec4d18ae512a152d6da314f9b1336dacdeb311222e2118e6e83ae`；
- 冻结后一次性读取2026；Q1、H1、M1-M8均与EX04相同，严格胜出条件全部FAIL；因果审计PASS；
- EX04继续作为研究基线，`baseline_20260823`继续作为通用回测正式基线。

权威文件：

```text
experiments/0824_EX08/03_execution.md
experiments/0824_EX08/04_conclusion.md
experiments/0824_EX08/artifacts/study_summary.json
experiments/0824_EX08/artifacts/trial_ranking.csv
experiments/0824_EX08/artifacts/holdout_metrics.json
experiments/0824_EX08/artifacts/batch_timings.csv
experiments/0824_EX08/artifacts/causal_audit.json
```

EX04/EX06协议输入与EX08冻结文件的研究身份哈希使用固定CRLF检出字节；`.gitattributes`已显式固定这些文件的`eol=crlf`。实验清单仍按LF归一化文本计算可移植文件记录，两种哈希用途不同，不得相互替代。

### 4.3 0825_EX01最终证据

本实验没有重跑Optuna，只对EX08已提交的4096个Trial按三套规则重排：

- 稳健优先Top 3：3123、2806、629；
- 胜出数优先Top 3：2385、2806、2478；
- 平均收益优先Top 3：3384、1192、2214；
- Trial 2806重复入选，共冻结8个唯一候选；冻结前未访问2026；
- 正式补测只加载一次2026行情与因子矩阵，8个候选全部执行Q1、H1、M1-M8三个窗口；
- 8个候选全部FAIL，因果审计全部PASS；没有候选在三个窗口都严格超过EX04；
- 2026报告排名第一为Trial 629：Q1收益13.9236%，超过EX04 7.3075个百分点；H1和M1-M8分别落后15.2333和23.4752个百分点；
- Trial 3123：Q1胜出0.8173个百分点，H1落后0.8959个百分点，M1-M8落后38.0287个百分点；
- 平均收益优先三项在2026均为0/3窗口胜出，表明样本内平均收益优势外推失败；
- EX04继续作为研究基线，`baseline_20260823`继续作为通用回测正式基线。

权威文件：

```text
experiments/0825_EX01/03_execution.md
experiments/0825_EX01/04_conclusion.md
experiments/0825_EX01/artifacts/selection_rule_top3.csv
experiments/0825_EX01/artifacts/frozen_candidates.json
experiments/0825_EX01/artifacts/holdout_metrics.json
experiments/0825_EX01/artifacts/holdout_ranking.csv
experiments/0825_EX01/artifacts/causal_audits.json
```

### 4.4 0825_EX02最终证据

本轮只使用2020—2025数据诊断冻结EX04，不生成挑战者、不访问2026、不判策略PASS/FAIL：

- 32项固定局部扰动中27项满足预注册邻域稳定，分类为`broad_plateau`；入场4/4、离场4/4、权重19/24稳定，拒绝“精确小数参数碰巧踩中尖峰”的解释；
- 权重、阈值、结构、趋势、量价位置五个对象全部为`regime_dependent`，没有4/5年度稳定正向或稳定负向对象；
- EX04相对`baseline_20260823`在2022—2024收益领先，在2021和2025落后；五个年度持仓率都更低；
- 按T-1可知的60日状态，EX04相对基线的每日收益差合计：上涨-14.10个百分点、下跌+17.68个百分点、震荡+17.20个百分点，历史机制更接近防守型择时；
- 2021—2025共有46个持仓差异区间，最大区间占绝对贡献11.20%，Top 3合计24.11%，不存在单一区间主导；
- 20日配对移动区块bootstrap共2000次，EX04领先比例90.70%，差值95%区间为-22.93%至+137.54%，仍覆盖0，不能排除样本偶然；
- 首次执行发现全现金联盟被vectorbt报告为无限夏普，分类无效；修复只将零收益、零暴露、零订单联盟的夏普效用规范为0，活跃联盟非有限值仍报错。修复后全部数值有限，聚焦回归46项通过；
- 结论不支持继续微调EX04权重/阈值或直接删除因子组。若继续研究，应预注册“保留下跌/震荡防守收益并减少上涨机会损失”的新机制，优先先做跨标的或未来样本外推。

权威文件：

```text
experiments/0825_EX02/03_execution.md
experiments/0825_EX02/04_conclusion.md
experiments/0825_EX02/artifacts/metrics.json
experiments/0825_EX02/artifacts/classification.csv
experiments/0825_EX02/artifacts/local_geometry.json
experiments/0825_EX02/artifacts/window_comparison.csv
experiments/0825_EX02/artifacts/regime_attribution.csv
experiments/0825_EX02/artifacts/bootstrap_summary.json
experiments/0825_EX02/artifacts/difference_intervals.csv
```

### 4.5 0825_EX03最终证据

本轮只使用2020—2025数据解释EX04相对`baseline_20260823`在2021H1、2023H1、2025H2三个已确认落后窗口中的交易路径，不生成挑战者、不访问2026、不判策略PASS/FAIL：

- 六个冻结变体在2021H1—2025H2十个半年窗口独立回测；逐日路径账本1212行、基线持仓区间77段、重点决策事件44个；
- 十个窗口对数财富差全部闭合，最大绝对残差1.915e-15；没有未分类机会损失，所有数值有限；
- 预注册主分类为`mixed_path_failure`：入场族占池化机会损失48.64%，退出族占51.36%，不存在统一覆盖三个落后窗口的单一根因；
- 2021H1和2023H1由退出侧主导，份额分别65.39%和68.34%，主要表现为通用基线持仓区间内的持仓中断；2025H2由入场侧主导，份额78.31%，主要是迟入场；
- 使用EX04入场、历史冠军退出的因果混合状态机在三个落后窗口分别改善5.4235、2.8179和0.6589个百分点，是最强诊断线索；
- 该退出侧回退在七个对照窗口中只改善2021H2，其余六个全部恶化，2022H1最大恶化9.6861个百分点，因此不得把历史冠军退出逻辑整体移植到EX04；
- 三个落后窗口没有纯`weight_block`；漏入场以阈值阻断为主，提前退出以联合边际阻断为主，但2023H1存在双重阻断，来源不够一致，不能直接调单个权重或阈值；
- 当前不要求先做跨标的外推，因为项目接受588080标的独立策略；下一步仍应先诊断退出侧收益为何只在特定窗口有效，而不是直接优化。

权威文件：

```text
experiments/0825_EX03/03_execution.md
experiments/0825_EX03/04_conclusion.md
experiments/0825_EX03/artifacts/metrics.json
experiments/0825_EX03/artifacts/mechanism_classification.json
experiments/0825_EX03/artifacts/variant_window_metrics.csv
experiments/0825_EX03/artifacts/daily_path_ledger.csv
experiments/0825_EX03/artifacts/mechanism_contributions.csv
experiments/0825_EX03/artifacts/decision_events.csv
experiments/0825_EX03/artifacts/hybrid_counterfactuals.csv
```

### 4.6 0825_EX04最终证据

本轮复用0825_EX03受跟踪档案中的20个`early_ex04_exit`事件，只加载截至2025的行情做事件级继续持有反事实；不生成挑战者、不访问2026、不判策略PASS/FAIL：

- 20次离场分歧中错误5次、保护11次、中性4次；多数离场具有真实保护价值；
- 5次错误离场总机会损失0.121389对数财富单位，Top 1占36.62%，Top 3占85.35%，机器分类为`path_concentrated_exit_failure`；
- 三个主导错误事件为2021-06-16、2023-02-08、2021-07-06，分别属于2021H1、2023H1、2021H2；全部是离场后很快重新入场的短暂持仓中断；
- 下跌状态4/4为保护性离场，`independent_double_block`的9个材料性事件9/9为保护性；长持仓年龄上下文也达到保护性门槛；
- 没有合格错误离场上下文；最接近的`joint_margin_block`错误率为5/7=71.43%，低于75%门槛；
- 得分和阈值距离的池化Cliff's delta虽为0.7818，但跨两个可比较窗口方向不一致；14个连续特征没有稳定大效应；
- 2025H2唯一离场事件的继续持有优势为0.4787%，低于0.5%材料门槛，分类中性；
- 反事实路径账本83行，最大闭合残差1.952e-18；20个事件、六个源文件清单哈希、18个2020—2025行情哈希全部通过审计。

研究含义：0825_EX03的退出侧收益由少数事件驱动，不能解释为离场信号整体质量差。当前不准全局放宽离场、增加确认日、延长持仓或把历史事件反推成门控；若继续，先对前三个主导事件做路径解剖，在形成新的独立假设前停止离场策略优化。

权威文件：

```text
experiments/0825_EX04/03_execution.md
experiments/0825_EX04/04_conclusion.md
experiments/0825_EX04/artifacts/metrics.json
experiments/0825_EX04/artifacts/exit_quality_classification.json
experiments/0825_EX04/artifacts/exit_event_counterfactuals.csv
experiments/0825_EX04/artifacts/exit_event_path_ledger.csv
experiments/0825_EX04/artifacts/exit_quality_by_context.csv
experiments/0825_EX04/artifacts/continuous_feature_separation.csv
```

### 4.7 0825_EX05最终证据

本轮当前研究基线明确为`0824_EX04`冻结策略，离场病灶和结果标签来自`0825_EX04`正式档案。对20个事件展开`0824_EX04`已有12个CZSC信号在`T-20…T`的因果轨迹，以两个2021病灶发现、2023病灶时间确认，并用11个保护性离场检查污染：

- 20个事件形成420行轨迹和127个预注册原子描述符；
- 两个2021病灶共享83个非恒定签名，53个通过2023确认，但低污染签名为0；
- 最接近门槛的是日线20日均线持续多头这一底层状态，仍命中3/11个保护性离场和1个中性离场，且不覆盖其余2个错误离场；
- 三个病灶最近10日都只出现1日低于离场阈值，但该事实也命中4个保护性和3个中性离场，不能推出“单日跌破不离场”；
- 两个2021病灶独有多周期方向不一致和周线中性，保护性污染为0，但2023病灶为三周期同向、周线向下，时间确认失败；
- 机器分类为`shared_but_contaminated_anatomy`：病灶共有表象很多，但现有12信号没有独特、低污染的共同指纹；
- 信号日重算得分最大误差8.500e-17，18个行情哈希只覆盖2020—2025，无候选、订单、冻结策略或2026访问。

研究含义：停止在`0824_EX04`同一12信号上继续拼离场条件、调阈值或加确认日。若继续离场研究，必须先预注册一个真正新增的事前表示；也可转向独立的2025H2入场问题。两条路线均不得把`0825_EX05`结果反馈补特征。

权威文件：

```text
experiments/0825_EX05/03_execution.md
experiments/0825_EX05/04_conclusion.md
experiments/0825_EX05/artifacts/metrics.json
experiments/0825_EX05/artifacts/anatomy_classification.json
experiments/0825_EX05/artifacts/event_descriptor_matrix.csv
experiments/0825_EX05/artifacts/confirmed_atomic_signatures.csv
experiments/0825_EX05/artifacts/dominant_event_panels.svg
```

### 4.8 0825_EX06最终证据

本轮只使用2020—2025数据检验28个预注册新增描述符，不生成挑战者、不访问2026、不判策略PASS/FAIL：

- 20个事件形成560行事件描述符矩阵，原始值全部有限，所有最大输入日期不晚于信号日；
- 2021发现8个共同分位签名，6个通过2023时间确认，2个达到表面低污染门槛；
- `downside_semivol5_to20=Q5`命中三个主导错误、1个保护、另1个错误和3个中性，不命中下跌保护或保护性joint-margin；
- `weekly_close_to_ma10=Q5`命中三个主导错误和2个保护，不命中下跌保护、保护性joint-margin、其他错误或中性；
- 唯一证据完整性失败为`2021-06-16 / ma20_slope5`只有117个有限历史值，低于预注册120日；
- 364种伪标签组合中60种也能产生合格签名，精确p值`0.164835`高于0.05；即使忽略单格历史不足，两个漂亮签名也未通过多重比较校正；
- 机器分类为`insufficient_new_representation_evidence`，不得降低门槛、重分箱或直接设计离场门控；
- 18个行情哈希只覆盖2020—2025，无订单、候选、冻结挑战者或holdout结果。

研究含义：连续波动和周线位置比原12个类别信号更接近病灶，但当前3个主导正例不足以排除偶然。当前不应继续围绕这两个表象调离场规则；若没有独立验证样本或真正独立信息，应优先转向2025H2入场问题。

权威文件：

```text
experiments/0825_EX06/03_execution.md
experiments/0825_EX06/04_conclusion.md
experiments/0825_EX06/artifacts/metrics.json
experiments/0825_EX06/artifacts/representation_classification.json
experiments/0825_EX06/artifacts/event_new_representation.csv
experiments/0825_EX06/artifacts/confirmed_signatures.csv
experiments/0825_EX06/artifacts/permutation_audit.json
```

### 4.9 0826_EX01最终证据

本轮只复用`0825_EX03`受跟踪的2021—2025逐日路径和决策事件，不访问2026、不生成候选：

- 45个通用基线做多而EX04空仓的连续区间中，44个能够一对一连接信号日决策事件；1个期间初始区间因缺少窗口内信号状态而审计排除；
- 44个事件中43个具有合格的事前市场状态；固定枚举49条简单规则（含永不偏离EX04的回退规则）；
- 按2021—2025逐年留一，只有两折选出非回退规则，且两条规则不同；其余三折回退EX04；
- 留出匹配只有5个事件且只覆盖2024一年，池化行动价值为`-0.017513`，最大单一正收益事件占76.05%；
- 跨折一致、五年支持、最小支持、五年全正、精确符号检验、池化正值和非单事件主导七项门槛全部失败；
- 机器分类为`no_stable_boundary`。

研究含义：在现有历史数据、事前信息和EX04/旧通用规则所张成的动作空间内，没有证据支持继续修改EX04。结合此前大规模搜索和病灶诊断，0824_EX04已晋升为当前588080正式活动基线；新证据只能来自前向时间或真正独立信息。

权威文件：

```text
experiments/0826_EX01/03_execution.md
experiments/0826_EX01/04_conclusion.md
experiments/0826_EX01/artifacts/frontier_events.csv
experiments/0826_EX01/artifacts/fold_selection.csv
experiments/0826_EX01/artifacts/boundary_classification.json
experiments/0826_EX01/artifacts/metrics.json
```

## 5. 代码结构与入口

### 5.1 唯一稳定入口

安装项目后只使用`czsc-trader`命令；旧Python脚本入口和runner模块CLI已经删除。

| 命令 | 职责 |
|---|---|
| `czsc-trader data prepare/validate` | 获取、后复权、发布或验证A股股票与ETF行情 |
| `czsc-trader baseline list/show/validate` | 查看并验证不可变基线注册表 |
| `czsc-trader backtest run` | 对已准备证券执行冻结基线普通回测 |
| `czsc-trader experiment run` | 只运行尚未冻结的预注册实验目录 |
| `czsc-trader experiment replay` | 在源档案之外隔离复现冻结实验 |
| `czsc-trader archive validate` | 验证一个或全部Git研究档案 |

默认`stdout`只输出一份JSON，进度和诊断进入`stderr`；`--format text`用于人工阅读。所有命令从当前目录向上发现仓库，也可显式传入`--repo-root`。

普通回测输出目录固定为 `outputs/<证券代码>_<MMDD>_RXX`。未提供收益目标时只输出指标，验收状态为 `N/A`。

### 5.2 研究实现模块

| 模块 | 职责 |
|---|---|
| `attribution.py` / `attribution_runner.py` | EX02冠军归因 |
| `four_layer.py` / `four_layer_runner.py` | EX03四层等价架构与权重阈值搜索 |
| `return_only_runner.py` | EX04收益率单目标搜索 |
| `factor_discovery.py` / `factor_discovery_runner.py` | EX05—EX06因子生成、事件支持和并行搜索 |
| `optuna_search.py` / `optuna_runner.py` | EX07—EX08参数投影、ask/evaluate/tell、SQLite/纯内存存储和TPE搜索 |
| `top3_holdout_runner.py` | 0825_EX01复用EX08 Trial输出进行三规则Top 3冻结与批量2026验收 |
| `ex04_attribution_runner.py` | 0825_EX02冻结EX04的组件、因子组、局部几何、路径、市场状态和bootstrap诊断 |
| `ex04_path_attribution_runner.py` | 0825_EX03冻结EX04的逐日路径、持仓区间、决策阻断和因果混合状态机归因 |
| `exit_signal_diagnosis_runner.py` | 0825_EX04离场事件继续持有反事实、上下文、连续特征和集中度诊断 |
| `dominant_exit_anatomy_runner.py` | 0825_EX05主导离场病灶的因果轨迹、原子签名、时间确认和保护性污染诊断 |
| `new_exit_representation_runner.py` | 0825_EX06新增连续描述符、因果分位、顺序确认、污染检查和精确标签枚举 |
| `decision_boundary_runner.py` | 0826_EX01分歧区间、按年留一规则选择和稳定边界分类 |
| `baseline_execution.py` | 统一执行归档固定规则与活动四层冻结基线 |
| `experiment_archive.py` | 实验目录、清单生成和哈希验证 |

数值runner不再包含参数解析或`main`入口。显式研究处理器注册表把协议类型映射到实现。已有实验是冻结档案，`experiment run`会在加载研究数据前拒绝；若要复现，必须使用`experiment replay`和源目录之外的新输出目录。新研究仍须先创建新的`MMDD_EXX`目录并预注册协议。

## 6. 数据状态

588080受跟踪行情覆盖：

- 起点：2020-11-16（上市日）；
- 当前数据末日：2026-08-24；
- 频率：30分钟、日线、周线；
- 复权：后复权 `hfq`；
- 当前验证：`data/raw/588080_validation.json` 为PASS。

新设备必须读取以下文件确认实时状态，不要仅相信本文日期：

```text
data/raw/588080_manifest.json
data/raw/588080_validation.json
```

仓库还保存159352、159516、515050的已准备数据，可用于普通基线回测；588080仍是当前策略研究唯一锚定标的。

## 7. 新设备恢复步骤

### 7.1 克隆与环境

```powershell
git clone https://github.com/tomxiao/czsc_trader.git
cd czsc_trader
git switch master
git pull --ff-only origin master
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip check
```

关键依赖以 `pyproject.toml` 为准，当前包括：

- Python >= 3.12；
- CZSC 1.0.1；
- vectorbt 1.1.0；
- Plotly 6.9.0；
- Joblib 1.5.3；
- Optuna 4.9.0。

SQLite由Python标准库提供，不需要单独安装。

### 7.2 接手前核验

```powershell
git rev-parse --show-toplevel
git status --short --branch
git log -5 --oneline --decorate
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

测试集只保留统一CLI的端到端功能验证，覆盖行情校验、基线、普通回测、实验档案、冻结保护和隔离回放；历史研究算法与内部实现不再保留TDD阶段单元测试。交付时运行完整的唯一测试命令，不得用局部测试代替。

### 7.3 验证实验档案

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all
```

该命令只验证Git档案完整性，不重新执行耗时研究。

### 7.4 普通回测示例

```powershell
.\.venv\Scripts\czsc-trader.exe backtest run `
  --symbol 588080.SH --asset etf `
  --targets configs\backtest_targets\588080_2026.json
```

未指定`--baseline`时，588080默认使用`baseline_20260826`。上述2026目标文件现在只用于已见历史表现复算，不构成独立验收。必须读取命令本次返回的`output_dir`，不得假设新设备存在任何历史输出目录。

### 7.5 正式研究与隔离复现

冻结的EX07—EX08只能验证或隔离复现，不得原地重跑：

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --archive experiments\0824_EX08
.\.venv\Scripts\czsc-trader.exe experiment replay `
  --dir experiments\0824_EX08 `
  --output replays\0824_EX08
```

新实验必须先创建新的预注册目录，并在协议中声明已注册`handler`或受支持的`experiment_type`，再执行`czsc-trader experiment run --dir <新目录>`。存储方式、Trial要求、停止条件和TPE参数由协议与处理器共同校验；正式执行仍须从干净且已提交的代码状态启动。

## 8. Git与研究工作流

1. 默认在 `master` 做轻量修改和bugfix。
2. 重量级开发或正式研究先询问用户是否新建分支；禁止使用Git worktree。
3. 开工前检查并保护用户已有修改。
4. 实验目录按 `MMDD_EXX` 命名；日期变化后编号从EX01重新开始，同一天单调递增。
5. 每轮只研究一个可证伪假设，先冻结目标、数据边界、候选空间、排序和停止条件。
6. 2026已经在项目层面观察，不得再作为新实验的独立留出；新的前向证据从2026-08-26之后开始。
7. PASS、FAIL、ERROR和诊断实验均须保留完整档案并纳入Git。
8. `outputs/`和运行态SQLite不得纳入研究提交。
9. 合并和推送后比较本地 `master` 与 `origin/master` SHA。

## 9. 下一轮研究建议

EX08和0825_EX01拒绝“扩大同一搜索或重排同一Trial集合”；0825_EX02说明`0824_EX04`附近是局部平台；0825_EX03—0825_EX06逐步证明局部入退场病灶缺少稳定、低污染的新表示；0826_EX01进一步证明简单事前决策边界不能跨年稳定选择何时偏离EX04。

下一步优先级：

1. `baseline_20260826`保持冻结，不再根据2020—2026已见历史修改12因子、权重、阈值或状态机；
2. `baseline_20260823`只用于显式历史复现，不再参与当前冠军判断；
3. 从2026-08-26之后开始积累前向表现、信号事件、成交和偏差证据；
4. 没有足够新样本前，不启动新的参数搜索、局部病灶修补或2026补测；
5. 若未来引入真正独立的新信息，必须另立预注册实验，且不能把已见历史反馈为同轮规则；
6. 项目允许588080使用标的独立策略，其他标的不得默认套用该活动基线。

这只是下一轮讨论起点，不是已批准协议。新会话不得直接启动优化或访问2026调参，必须先与用户确认目标和实验设计。

## 10. 新会话接手清单

1. 完整阅读本文。
2. 确认仓库根目录、分支、远端和工作区状态。
3. 从`registry.json`确认`baseline_20260826`为588080活动基线、`baseline_20260823`为归档基线。
4. 验证588080行情manifest和validation。
5. 验证0824_EX01—0826_EX01实验清单，不依赖历史outputs。
6. 阅读0824_EX04、0824_EX06、0824_EX07和0826_EX01的目标、设计和结论。
7. 修改后运行完整CLI端到端测试；新增测试只覆盖新的用户可见功能链路，不测试内部实现细节。
8. 研究任务先预注册；普通回测只加载冻结规则。
9. 实盘账户状态与回测状态完全分离；没有明确成交回报时一律按未成交处理。
10. 所有结果明确标注样本选择期、冻结点、已见历史范围和前向验证起点。

## 11. 可复制给新会话的提示词

```text
请接手当前Git仓库中的588080 CZSC策略研究。请始终按跨设备新会话处理，不要假设任何绝对路径、历史outputs、虚拟环境、SQLite运行库或未提交文件存在。

先执行只读检查：
1. git rev-parse --show-toplevel
2. git status --short --branch
3. git log -5 --oneline --decorate

然后完整阅读：
1. docs/RESEARCH_HANDOFF.md
2. configs/rule_baselines/registry.json
3. experiments/0824_EX04/{01_goal.md,02_design.md,04_conclusion.md}
4. experiments/0824_EX06/{01_goal.md,02_design.md,04_conclusion.md}
5. experiments/0824_EX07/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md}
6. experiments/0824_EX08/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md}
7. experiments/0825_EX01/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md}
8. experiments/0825_EX02/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md}
9. experiments/0825_EX03/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md}
10. experiments/0825_EX04/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md}
11. experiments/0825_EX05/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md}
12. experiments/0825_EX06/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md}
13. experiments/0826_EX01/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md}

重要边界：baseline_20260826是588080当前活动正式基线，与0824_EX04冻结策略逐字节同源；baseline_20260823只用于显式历史复现，不再作为默认策略或当前冠军。EX07完成608个SQLite Optuna Trial，EX08完成4096个纯内存Trial；0825_EX01补测8个候选，均未超过EX04；0825_EX02—0825_EX06完成机制、路径、离场和新增表示诊断；0826_EX01对44个分歧区间和49条简单规则做按年留一，分类为no_stable_boundary。现有证据支持停止利用2020—2026已见历史修补EX04。研究事实只认Git跟踪的experiments档案；outputs只用于当前设备新运行的普通回测。2026已经在项目层面观察，不能再作为独立留出；新的独立前向证据从2026-08-26之后开始。

默认在master做轻量开发；重量级研究先询问是否新建分支；禁止使用git worktree。保护所有已有修改。只在任务需要时运行回测或研究，并使用命令实际返回的目录。研究结果无论PASS还是FAIL都必须如实归档。实盘账户没有明确成交回报时一律按未成交处理。
```
