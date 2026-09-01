# 588080 CZSC策略研究：跨机新会话交接

> **最高优先级约束：始终按跨设备接力处理。**
>
> 不得假定新会话与上一会话位于同一台设备、同一绝对路径，或保留任何未提交文件。不得依赖历史 `outputs/`、本地SQLite数据库、虚拟环境和终端输出。研究事实只以Git跟踪的源码、配置、`data/raw/*_manifest.json`、`data/raw/*_validation.json` 和 `experiments/MMDD_EXX/` 为准。`outputs/`只用于普通回测输出，不是研究档案；需要结果时必须在当前设备重新运行，并使用命令实际返回的目录。

本文档记录截至 **2026-09-01 / 0901_EX21** 的可接力状态。0901_EX05—EX18已纳入`master`；0901_EX19—EX21在独立研究分支完成regime条件权重挑战、协议纠错和六候选三窗口评分，但尚未合并或推送。新会话仍应先验证仓库当前状态，不能把本文中的分支、提交或日期描述当成未经核验的实时事实。

## 1. 当前目标与两个基线

### 1.1 长期研究目标

- 标的固定为 `588080.SH`；
- 当前数据覆盖上市日至2026-08-31；
- 截至2026-08-31的数据已经被项目观察，全部属于已见历史证据；
- `0824_EX04`已冻结为当前588080正式基线，不再利用已见历史样本继续回溯修补；
- 新的未观察前向行情从2026-08-31之后开始；
- 后续若开展新研究，必须预注册独立信息和验证边界，不得把2026重新包装成留出样本。

此前“单次实验内冻结后一次2026验收”的规则仍是历史实验事实，但截至当前，2026-08-31及以前结果已经被观察，不能再承担新的独立样本外角色。未来若需要新的未观察前向证据，应从2026-08-31之后积累；已见历史仍可用于透明开发和诊断，但不能被重新描述为新留出。

0901_EX19错误增加了用户未授权的年度3/5否决门槛，原始FAIL档案保持不可变，但不能作为终止路线的依据。0901_EX20保持同一625项空间并删除该门槛，只按连续2021—2025最大回撤、卡玛和盈亏比共同改善筛选，冻结候选143后执行锁定至2026-08-28的测试。挑战者卡玛、盈亏比、收益率和夏普率均更高，但最大回撤与活动基线相同，未满足三项全部严格改善，正式FAIL；活动基线不变。

0901_EX21固定EX20全部6个合格候选，对2026Q1、H1和M1—M8的最大回撤、卡玛、盈亏比逐格按胜1、平0.5、负0计分。125、143、275、293均为8.0分并列第一，418为4.5分，400为2.5分；活动基线自比较基准分为4.5。该排名使用已见2026，只是诊断证据，不自动晋升策略。

### 1.2 当前活动正式基线

588080普通回测默认加载：

```text
configs/rule_baselines/baseline_20260826.json
```

实际默认版本由`configs/rule_baselines/registry.json`的`latest`决定。`baseline_20260826`与`experiments/0824_EX04/artifacts/frozen_challenger.json`逐字节相同，是12因子四层策略，允许用于所有已验证标的的普通回测。它的调参与研究证据仍只来自`588080.SH`；其他标的结果不自动构成外推有效性证据。该基线最初从2026-08-26开始积累前向记录，但截至2026-08-31的行情现已被观察；当前新的未观察前向行情只能从2026-08-31之后开始。

### 1.3 历史归档基线

原通用固定规则：

```text
configs/rule_baselines/baseline_20260823.json
```

`baseline_20260823`曾是通用回测正式基线，但其形成过程使用了2026数据，现已降为`archived`。它只允许通过显式版本号复现历史，不能作为默认回测、当前冠军或新的独立比较对象。历史实验继续保留当时对它的真实引用，不做事后改写。

| 名称 | 用途 | 当前身份 |
|---|---|---|
| `baseline_20260826` | 默认普通回测、588080后续前向比较 | 通用活动基线，来源为0824_EX04；跨标的可用不等于已证明通用有效 |
| `baseline_20260823` | 显式历史复现 | 归档基线，不再默认使用 |
| `experiments/0824_EX04/.../frozen_challenger.json` | 新基线来源与历史实验研究对象 | 冻结源档案，禁止改写 |

### 1.4 0901终局研究产生的历史挑战者

`experiments/0901_EX13/artifacts/frozen_historical_challenger.json`是在2021—2025已见历史上胜出的新挑战者：它保留`baseline_20260826`的12个因子及相对权重、阈值和状态机，加入一个0.20权重的日线CZSC风险缓解事件，并将原12因子权重同比缩放至总计0.80。完整规则只能从该冻结文件读取。

它在同一连续窗口、费用和执行规则下同时提高收益、降低最大回撤，但这不是独立样本外证明。2026没有参与生成、筛选或优化；EX15随后对冻结规则执行2026回看，因全年截至8月28日没有出现新增事件，两套策略路径和指标完全相同，未获得晋升证据。该文件尚未写入`configs/rule_baselines/registry.json`，普通回测默认仍使用`baseline_20260826`。只有在2026-08-28之后的新行情积累足够前向证据后，才能另行研究是否晋升。

## 2. 不可违反的回测边界

1. 行情只能来自 `data/raw` 中已准备并通过验证的30分钟、日线和周线CSV。
2. A股股票和ETF数据统一使用Tushare后复权价格，复权方式为 `hfq`，因子来源为 `fund_adj`。
3. 588080的30分钟数据仅对2024年七个已确认的Tushare异常交易日做硬编码成交量修正；日期记录在 `data/raw/588080_manifest.json`。
4. 因子日 `T` 只能读取不晚于 `T` 收盘的已完成数据，目标仓位最早在下一交易日开盘成交。
5. 当前活动正式基线日频只做多或空仓，目标仓位为0或1；预注册仓位实验可显式使用0到1之间的目标值。回测中的`exposure`是目标仓位的时间均值，二元基线下等同于满仓交易日占比。
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

## 4. 0824_EX01—0901_EX18研究进度

每轮实验的完整事实均位于 `experiments/<实验编号>/`：

- `01_goal.md`：目标与PASS边界；
- `02_design.md`：预注册设计；
- `03_execution.md`：实际执行过程；
- `04_conclusion.md`：结论；
- `experiment_manifest.json`：受跟踪文件哈希和实验元数据；
- `artifacts/`：协议、候选、指标、订单、审计和冻结策略。

| 实验 | 性质 | 关键变化 | 结论 |
|---|---|---|---|
| 0824_EX01 | 冠军挑战 | 再入场冷却期与门控 | FAIL；没有冻结挑战者，未访问2026 |
| 0824_EX02 | 诊断归因 | 因子、状态、映射、权重、阈值和状态机反事实 | COMPLETE；不判PASS，不产生挑战者 |
| 0824_EX03 | 架构实验 | 固定冠军12因子，改为四层架构并优化权重/阈值 | FAIL；风险调整表现改善，但2026收益未胜出 |
| 0824_EX04 | 收益单目标 | 固定12因子，优化权重和阈值 | FAIL于历史冠军；其冻结策略被指定为后续研究基线 |
| 0824_EX05 | 因子扩展 | 状态展开，18个非零因子 | FAIL；只在2026H1收益超过EX04 |
| 0824_EX06 | 因子缺陷修复 | 事件感知候选、91因子、Joblib并行搜索 | FAIL；候选空间更合理，但搜索结果未超过EX05 |
| 0824_EX07 | 搜索算法实验 | 固定EX06的91候选，用Optuna联合搜索子集、权重和阈值 | FAIL；608个Trial中没有新Trial超过EX04 |
| 0824_EX08 | 搜索深度/执行效率实验 | EX07同一搜索空间与TPE配置，改为纯内存并固定完成4096个Trial | FAIL；最佳仍为Trial 0（EX04），没有新方案超过EX04 |
| 0825_EX01 | EX08输出补测 | 三套规则各取Top 3，共8个唯一Trial，冻结后批量执行一次2026验收 | FAIL；8项全部未能在三个窗口严格超过EX04，审计全部PASS |
| 0825_EX02 | EX04机制归因 | 权重/阈值分解、因子组联盟、32项局部扰动、路径/状态归因和区块bootstrap | COMPLETE；EX04是状态依赖的防守型局部平台，不是精确参数尖峰，仍不能排除样本偶然 |
| 0825_EX03 | EX04交易路径归因 | 三个落后半年窗口的精确财富账本、入退场机制、决策阻断与因果混合状态机 | COMPLETE；主分类`mixed_path_failure`，退出侧是更强线索但不能全局回退退出逻辑 |
| 0825_EX04 | EX04离场信号诊断 | 20个离场分歧事件的继续持有反事实、上下文区分、连续特征和路径集中度 | COMPLETE；`path_concentrated_exit_failure`，多数离场有效，少数错误离场主导损失，禁止直接优化 |
| 0825_EX05 | 主导错误离场病灶解剖 | 展开`0824_EX04`的12个原始CZSC信号在20个离场前`T-20…T`的因果轨迹 | COMPLETE；`shared_but_contaminated_anatomy`，53个跨时间共同签名全部污染保护性离场，现有表示不足 |
| 0825_EX06 | 新离场表示诊断 | 预注册28个连续结构/能量/量价描述符，顺序确认并做364种精确标签枚举 | COMPLETE；`insufficient_new_representation_evidence`，2个表面低污染签名未通过多重比较校正，禁止晋升 |
| 0826_EX01 | 决策边界稳定性诊断 | 44个分歧区间、49条简单规则、按年留一 | COMPLETE；`no_stable_boundary`，停止利用已见历史继续回溯修补 |
| 0829_EX01 | 入场定仓冠军挑战 | 冻结活动基线，仅测试`0/0.5/1`三级仓位和三个满仓阈值；持有期间不调仓 | FAIL；冻结`full_0.250`，选择期3/8胜出，三个原2026窗口与冠军路径及指标完全相同，因未严格胜出而失败；审计PASS，基线不变 |
| 0829_EX02 | 动态仓位冠军挑战 | 入场全仓；原最短持有期后，按原入场/退出阈值在全仓、半仓和空仓间转换；无新阈值、无候选选择 | FAIL；可见半年4/8胜出；2026Q1收益提高1.2291个百分点，但2026H1和M1-M8分别落后0.2959、0.8756个百分点；审计PASS，基线不变 |
| 0830_EX01 | 下行风险仓位冠军挑战 | 以2021—2025选择27组下行半波动率压力仓位，只有收益不下降且最大回撤严格改善才允许冻结并访问2026 | ERROR；日线日期索引对齐错误导致候选计算中止；未完成选择、未访问2026，基线不变 |
| 0830_EX02 | EX01技术重试 | 修复日期索引后逐字保持同一数值协议，重新执行27组候选选择 | FAIL；没有候选同时满足收益不下降和最大回撤严格改善；未冻结挑战者、未访问2026，基线不变 |
| 0830_EX03 | 五轮仓位风险研究第1轮 | 只用2021—2023，对持仓期趋势损伤、连续下跌与日内抛压做5日和10日后验归因 | COMPLETE；只有日内抛压在两个周期的事件后平均收益均为负，本轮仅诊断，不产生候选 |
| 0830_EX04 | 五轮仓位风险研究第2轮 | 预注册12组趋势损伤压力仓位 | FAIL；0项同时满足收益不下降且最大回撤严格改善，未访问2024以后数据 |
| 0830_EX05 | 五轮仓位风险研究第3轮 | 预注册6组连续下跌压力仓位 | FAIL；0项同时满足收益不下降且最大回撤严格改善，未访问2024以后数据 |
| 0830_EX06 | 五轮仓位风险研究第4轮 | 预注册8组日内抛压压力仓位 | PASS（发现段）；8项均通过2021—2023门槛，冻结`intraday_C0.35_V0.60_P0.50`供第五轮锁定验证 |
| 0830_EX07 | 五轮仓位风险研究第5轮首次执行 | 锁定家族胜者，在2024—2025验证后才允许访问2026 | ERROR；验证窗口起点`InitialEntry`审计事件集错误；已访问2024—2025，未访问2026，不消耗有效实验额度 |
| 0830_EX08 | EX07等协议技术重试 | 只修复区间起点事件审计，候选、参数、门槛、排序和数据边界不变 | FAIL；唯一候选在2024—2025收益由74.0632%降至68.8640%，最大回撤由-20.1442%恶化至-21.0651%；未冻结最终候选、未访问2026，基线不变 |
| 0901_EX01 | CZSC因子稳定性诊断 | 新增21个日线结构信号，分离状态与稀疏事件；2021—2023发现、2024—2025锁定验证 | PASS（因子发现）；52个规范因子中35个满足支持度，11个冻结候选中2个形式通过；`BI涨跌幅向上`是暂定风险状态，`二卖`受少数样本驱动而不建议直接晋升；因果重放PASS，未访问2026，未形成策略 |
| 0901_EX02 | 笔方向×力度层级诊断 | 对`BI涨跌幅`的向上/向下与第1—5层做条件拆分，避免把多值结构压成单一方向 | FAIL；10个固定候选中5个满足支持度，但年度效果全部变号，没有候选进入锁定验证；未访问2024以后数据，不形成策略 |
| 0901_EX03 | 状态持续年龄诊断 | 在2021—2025已见历史上检验`BI涨跌幅向上`连续年龄及固定年龄分箱 | COMPLETE；状态最长仅8日，早期与中期效果跨年反转，原状态效应不能由持续年龄稳定解释；因果重放PASS，不形成策略 |
| 0901_EX04 | 条件增量有效性诊断 | 控制冠军已有日线笔方向，检验`BI涨跌幅`有效输出是否提供额外风险信息 | COMPLETE；向上候选的20日最大回撤差仅4/5年同号，向下候选在2023—2024反向，二者均为`no_stable_incremental_validity`；因果重放PASS，不形成策略 |
| 0901_EX05 | CZSC终局研究总计划 | 冻结统一信息空间、准入门槛、多重比较、两轮策略预算和路线停止条件 | COMPLETE；第一轮策略集成找到历史挑战者，第二轮按协议不执行；未读取2026 |
| 0901_EX06 | 原子结构家族 | 120个信号配置形成899个规范因子 | PASS（形式准入117）；全局多重比较后无幸存者 |
| 0901_EX07 | 稀疏事件家族 | 30个配置形成81个事件因子，买卖点不再受连续状态覆盖率门槛误杀 | PASS（形式准入4）；全局多重比较后无幸存者 |
| 0901_EX08 | 状态动态家族 | 对原子/事件构造首次出现、离开、年龄和事后状态，共3,491个规范因子 | PASS（形式准入259）；唯一全局幸存者是日线BI涨跌幅第1层状态离开事件 |
| 0901_EX09 | 跨周期家族 | 30分钟、日线、周线同类结构关系，276个规范因子 | PASS（形式准入32）；全局多重比较后无幸存者 |
| 0901_EX10 | 全局多重比较审计 | 对3,462个合格因子—端点假设执行HAC增量回归和全局BH校正 | PASS；412个形式准入因子只剩1个，FDR固定10% |
| 0901_EX11 | 冠军仓位条件确认 | 将唯一事件按冠军目标仓位0/1拆分并重新做共同门槛和确认性BH | PASS；空仓、持仓两个条件均确认风险缓解方向 |
| 0901_EX12 | 合格二阶交互 | 只组合EX11确认的两个条件事件 | FAIL；二者互斥，唯一AND交互恒为零，不构造高阶或逻辑补集挽救 |
| 0901_EX13 | 第一轮统一策略集成 | 新事件作为第13因子，固定冠军相对权重、阈值和状态机，搜索6个正权重 | PASS；权重0.20使连续五年收益76.9987%→92.5790%、最大回撤-25.5149%→-21.5957%、夏普0.7342→0.8259；审计PASS |
| 0901_EX14 | 路线终局判定 | 汇总覆盖矩阵、候选漏斗、父子哈希和挑战者边界 | COMPLETE；`HISTORICAL_CHALLENGER_FOUND`，活动基线不变，等待新前向证据 |
| 0901_EX15 | 冻结后2026回看 | 固定EX13规则，比较2026Q1、H1和1—8月，不搜索参数 | COMPLETE；159日计分全部不同、7日改变入场阈值分类，但状态机输出仓位差异0日，全部业绩指标相同；继续保留`baseline_20260826` |
| 0901_EX16 | EX13计分单调性首次诊断 | 固定五个分数档，检验持仓状态下未来收益和回撤单调性 | ERROR；仅一个分数出现`1.11×10^-16`浮点舍入差，档位、事件和目标完全一致；如实保留技术错误 |
| 0901_EX17 | EX16等协议技术重试 | 只将连续分数前缀审计改为绝对容差`1e-12` | FAIL；0/5年度满足支持度，高分档20日回撤反而恶化0.3054个百分点，HAC p=0.5085；未运行仓位候选 |
| 0901_EX18 | 计分分档仓位终局 | 汇总诊断、候选漏斗和停止条件 | COMPLETE；`SCORE_TIER_ROUTE_TERMINATED`，M1—M4全部未运行，EX13与活动基线身份均不变 |
| 0901_EX19 | Regime条件权重挑战 | 固定12因子与阈值，以T-1 ER60中位数划分趋势/震荡，对两档三因子组权重评估625项 | FAIL；连续窗口三指标同时改善6项，但年度双指标最多2/5，研究胜者0项；未冻结挑战者、未访问2026，活动基线不变 |
| 0901_EX20 | EX19协议纠错 | 删除无授权年度3/5否决门槛，同一625项只按连续窗口三指标筛选并冻结胜者 | FAIL；候选143的2026卡玛、盈亏比、收益和夏普更高，但最大回撤与基线相同；因果审计PASS，活动基线不变 |
| 0901_EX21 | 六候选三窗口评分 | 固定EX20的6个合格候选，按Q1/H1/M1—M8的回撤、卡玛、盈亏比执行九项胜平负计分 | COMPLETE；125、143、275、293同为8.0分并列第一，418为4.5分，400为2.5分；已见历史诊断，不自动晋升 |

### 4.1 如何读取历史证据

上表只负责给出研究路线、当前状态和档案索引，不替代正式实验记录。需要引用某轮实验的数值、判定规则或执行细节时：

1. 完整阅读该实验的`01_goal.md`、`02_design.md`、`03_execution.md`和`04_conclusion.md`；
2. 用`experiment_manifest.json`验证档案身份和文件哈希；
3. 从`artifacts/`读取机器可读证据，不从本文转抄历史数值；
4. 若历史档案中的基线身份与当前注册表不同，前者只表示实验当时状态，当前身份始终以`configs/rule_baselines/registry.json`为准。

研究链条可以概括为：0824_EX01—EX04建立并冻结EX04；0824_EX05—EX08扩大因子和搜索规模但未产生更优方案；0825_EX01—0826_EX01完成机制、路径、离场、新表示和边界诊断；0829_EX01—0830_EX08证明冻结冠军路径后的简单仓位覆盖层不能同时保持收益并改善回撤；0901_EX01—EX04暴露早期候选的稳定性不足；0901_EX05—EX14系统覆盖4,747个CZSC结构候选，经全局多重比较压缩到一个稳定风险缓解事件，并在统一四层架构第一轮集成中形成历史挑战者；EX15说明挑战者与冠军虽计分不同但2026仓位同路；EX16—EX18进一步证明EX13计分高低不具备足够的跨年仓位单调性；EX19暴露无授权年度门槛，EX20纠错后冻结候选143，EX21再把6个合格候选统一放入三窗口评分，形成四项8.0分并列第一。活动基线仍是`baseline_20260826`，所有挑战者均未自动晋升。

### 4.2 五轮仓位风险计划的最终判定

五个有效轮次是`0830_EX03`、`0830_EX04`、`0830_EX05`、`0830_EX06`和`0830_EX08`；`0830_EX07`是如实保留的技术ERROR，不另计额度。发现段固定为2021—2023，锁定验证段固定为2024—2025，2026仅允许验证胜者冻结后执行。三类仓位机制均为持仓期风险压力下减至0.5或0.75，信号日只使用当日已完成数据，下一交易日开盘执行。

唯一发现段胜者`intraday_C0.35_V0.60_P0.50`把发现段收益从1.6356%提高到5.5349%，并把最大回撤从-25.5149%改善到-22.1030%。但在未参与选择的2024—2025验证段，收益相对冠军下降5.1992个百分点，最大回撤恶化0.9210个百分点，因此不满足两个硬门槛中的任何一个。最终没有仓位方案晋升，活动基线仍为`baseline_20260826`；2026FULL未运行，不存在可报告的新候选2026结果。

这一结果证伪的是本计划预注册的趋势损伤、连续下跌和日内抛压三类机制及其参数范围，不等于证明所有仓位管理方法都无效。后续若继续研究仓位，必须引入不同且有金融机制依据的新信息或架构，不能对已失败网格做事后细化或组合。

## 5. 稳定入口与实现发现

安装项目后只使用`czsc-trader`命令；旧Python脚本入口和runner模块CLI已经删除。

| 命令 | 职责 |
|---|---|
| `czsc-trader data prepare/validate` | 获取、后复权、发布或验证A股股票与ETF行情 |
| `czsc-trader baseline list/show/validate` | 查看并验证不可变基线注册表 |
| `czsc-trader backtest run` | 对已准备证券执行冻结基线普通回测 |
| `czsc-trader experiment run` | 只运行尚未冻结的预注册实验目录 |
| `czsc-trader experiment replay` | 在源档案之外隔离复现冻结实验 |
| `czsc-trader archive validate` | 验证一个或全部Git研究档案 |

默认`stdout`只输出一份UTF-8 JSON，进度和诊断进入`stderr`；`--format text`用于人工阅读。所有命令从当前目录向上发现仓库，也可显式传入`--repo-root`。

普通回测默认输出到 `outputs/<证券代码>_<MMDD>_BTXX`，也可通过`--outputs-root`指定其他根目录；实际目录只认命令返回的`artifacts.output_dir`。普通回测只比较冻结策略与Buy & Hold的收益率、夏普率和最大回撤率及三项差值，不包含目标阈值或PASS/FAIL判定。

研究协议到实现的映射由`src/czsc_trader/research/registry.py`和各实验的`artifacts/protocol.json`共同决定。数值runner不提供独立CLI；跨机接力不得依赖本文维护易漂移的内部模块清单，实际代码位置应从当前提交和注册表发现。

行情适配器是位于`packages/dataflows/`的独立Python子项目，运行时导入名为`dataflows`且不得依赖`czsc_trader`。新设备使用`.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows -e ".[test]"`同时安装子项目和主项目；本地Tushare令牌只放在被Git忽略的仓库根目录`.env`中。安装后的CLI不得依赖仓库根目录出现在`PYTHONPATH`中。

## 6. 数据状态

588080受跟踪行情覆盖：

- 起点：2020-11-16（上市日）；
- 当前数据末日：2026-08-31；
- 频率：30分钟、日线、周线；
- 复权：后复权 `hfq`；
- 当前验证：`data/raw/588080_validation.json` 为PASS。

每个`data/raw/*_manifest.json`顶层都必须包含非空`name`，其值由Tushare证券基础信息接口返回；`data prepare`会在发布行情时自动写入，`data validate`会校验并返回该中文简称。当前已准备的4个标的是：

| 标的 | manifest中文简称 |
|---|---|
| `159352.SZ` | 南方中证A500ETF |
| `159516.SZ` | 国泰中证半导体材料设备主题ETF |
| `515050.SH` | 华夏中证5G通信主题ETF |
| `588080.SH` | 易方达上证科创板50成份ETF |

新设备必须读取以下文件确认实时状态，不要仅相信本文日期：

```text
data/raw/588080_manifest.json
data/raw/588080_validation.json
```

仓库还保存159352、159516、515050的已准备数据，可用于普通基线回测；588080仍是当前策略研究唯一锚定标的。

## 7. 新设备恢复步骤

### 7.1 克隆与环境

远端默认使用SSH：

```powershell
git clone git@github.com:tomxiao/czsc_trader.git
cd czsc_trader
git switch master
git pull --ff-only origin master
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows -e ".[test]"
```

关键依赖以`pyproject.toml`为准。SQLite由Python标准库提供，不需要单独安装；虚拟环境和本地SQLite运行库都不跨机复制。

### 7.2 接手前完整核验

在启动回测、修改或研究前依次执行：

```powershell
git rev-parse --show-toplevel
git status --short --branch
git log -5 --oneline --decorate
git remote get-url origin
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
.\.venv\Scripts\czsc-trader.exe baseline list
.\.venv\Scripts\czsc-trader.exe baseline validate `
  --version baseline_20260826 --symbol 588080.SH
.\.venv\Scripts\czsc-trader.exe archive validate --all
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

期望`origin`为SSH地址，活动基线为`baseline_20260826`，588080数据与全部实验档案验证为PASS。若任一事实不同，应以当前Git内容和命令输出为准，先查明差异，不得沿用本文快照继续执行。

测试集覆盖统一CLI端到端功能以及分数仓位、风险压力仓位状态机、区间起点资金账本和因果审计，包含行情校验、基线、普通回测、实验档案、冻结保护和隔离回放。

### 7.3 普通回测示例

```powershell
.\.venv\Scripts\czsc-trader.exe backtest run `
  --symbol 588080.SH --asset etf `
  --windows configs\backtest_windows\2026.json `
  --window 2026FULL
```

未指定`--baseline`时，所有标的默认使用`baseline_20260826`。窗口文件只定义日期，不绑定标的、不包含收益目标；`--window`只运行指定窗口。必须读取命令返回的`artifacts.output_dir`，不得假设新设备存在任何历史输出目录。

`configs/backtest_windows/2026.json`中的`2026FULL`左右边界都不是固定日期；普通回测会按当前标的，从已通过manifest与三周期校验的`data/raw`日线中取2026年第一个和最后一个有记录的交易日。历史研究代码、实验档案和`docs/baselines/588080_2026_expected.json`仍保留各自冻结时的数据边界，不随行情更新改写。

### 7.4 正式研究与隔离复现

有效`experiment_manifest.json`标识冻结档案，禁止原地重跑。隔离复现应写到仓库之外的新临时目录：

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate `
  --archive experiments\0824_EX08
$replayDir = Join-Path ([System.IO.Path]::GetTempPath()) `
  ("czsc-trader-0824_EX08-" + [guid]::NewGuid())
.\.venv\Scripts\czsc-trader.exe experiment replay `
  --dir experiments\0824_EX08 --output $replayDir
```

隔离回放产物不是研究证据。新实验必须先创建新的预注册目录，在协议中声明已注册`handler`或受支持的`experiment_type`，并按重量级研究规则确认分支后，才能执行：

```powershell
.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments\MMDD_EXX
```

存储方式、Trial要求、停止条件和TPE参数由协议与处理器共同校验；正式执行必须从干净且已提交的代码状态启动。

## 8. Git与研究工作流

1. 默认在 `master` 做轻量修改和bugfix。
2. 重量级开发或正式研究先询问用户是否新建分支；禁止使用Git worktree。
3. 开工前检查并保护用户已有修改。
4. 实验目录按 `MMDD_EXX` 命名；日期变化后编号从EX01重新开始，同一天单调递增。
5. 每轮只研究一个可证伪假设，先冻结目标、数据边界、候选空间、排序和停止条件。
6. 用户明确确认的目标与约束是唯一硬验收来源；辅助诊断默认只作展示。任何新增筛选、排序或PASS门槛必须先说明依据并取得用户明确确认，禁止为“稳健性”擅自加码。
7. 截至2026-08-31的行情已经在项目层面观察，不得再作为新实验的独立留出；新的未观察前向行情从2026-08-31之后开始。
8. PASS、FAIL、ERROR和诊断实验均须保留完整档案并纳入Git。
9. `outputs/`和运行态SQLite不得纳入研究提交。
10. 合并和推送后比较本地 `master` 与 `origin/master` SHA。

## 9. 下一轮研究建议

既有搜索和仓位覆盖层路线均未产生可晋升策略。EX20候选143在已见2026数据上显著改善卡玛和盈亏比，但最大回撤与活动基线相同，因此只保留为有价值的历史挑战者。0901_EX01重新检查CZSC信息源：`cxt_bi_zdf_V230601::向上`在锁定验证段的未来20日最大回撤差平均改善1.51个百分点，但收益方向不稳定；`cxt_second_bs_V230320::二卖`的验证均值优势受少数大涨样本驱动。0901_EX02没有找到稳定的方向×力度层级，0901_EX03没有找到稳定的状态年龄解释，0901_EX04在控制冠军已有日线笔方向后也没有找到五年同号的增量有效性。四轮结果共同表明：原始CZSC表示仍值得优先研究，但全样本差异不能直接视为可用的新信息。

下一步优先级：

1. `baseline_20260826`保持冻结，不再根据2020—2026已见历史修改12因子、权重、阈值或状态机；
2. `baseline_20260823`只用于显式历史复现，不再参与当前冠军判断；
3. 不再优化附着于冠军二元路径的独立仓位覆盖层；下一代挑战者应统一输出入场、离场和仓位；
4. 下一轮CZSC因子发现管线必须保留原始信号多值结构、把状态与稀疏事件分开，并在候选冻结前完成跨年稳健性、稀疏事件影响点和相对冠军已有信息的条件增量审计；
5. `BI涨跌幅向上`和`二卖`只作为已知线索保留，不得直接赋权；前者未通过条件增量门槛，后者未通过稀疏事件稳健性审计；
6. 只有通过上述准入门槛的新因子才能另开统一多档仓位实验；正式验收条件必须逐轮由用户明确确认，不得沿用或添加未经确认的门槛；
7. 从2026-08-31之后继续积累前向表现；截至该日的数据不得重新表述为独立留出；
8. `baseline_20260826`允许所有已验证标的默认用于普通回测，但其研究证据只来自588080。

这只是下一轮讨论起点，不是已批准协议。新会话不得直接启动优化或访问2026调参，必须先与用户确认目标和实验设计。

## 10. 新会话接手清单

1. 完整阅读本文和`configs/rule_baselines/registry.json`。
2. 按第7.2节完成Git、依赖、行情、基线、实验档案和端到端测试核验。
3. 完整阅读0824_EX04、0826_EX01、0829_EX01、0829_EX02、0830_EX01—EX08和0901_EX01—EX04的目标、设计、执行、结论及manifest。
4. 因子问题优先读0901_EX01—EX04，再补读0824_EX06—EX08和0825_EX01；归因或信号问题再补读0825_EX02—EX06。
5. 引用某轮实验的精确数值或规则前，完整阅读该轮四份文档、manifest和相关机器产物。
6. 研究任务必须先预注册；普通回测只加载冻结规则。
7. 修改后运行完整CLI端到端测试；新增测试只覆盖用户可见功能链路。
8. 实盘账户状态与回测状态完全分离；没有明确成交回报时一律按未成交处理。
9. 所有结果明确标注选择样本、冻结点、已见历史范围和前向验证起点。

## 11. 可复制给新会话的提示词

```text
请接手当前Git仓库中的588080 CZSC策略研究。始终按跨设备新会话处理，不要假设任何绝对路径、历史outputs、回放目录、虚拟环境、SQLite运行库、终端输出或未提交文件存在。

先执行只读检查：
1. git rev-parse --show-toplevel
2. git status --short --branch
3. git log -5 --oneline --decorate
4. git remote get-url origin

然后完整阅读核心文件：
1. docs/RESEARCH_HANDOFF.md
2. configs/rule_baselines/registry.json
3. experiments/0824_EX04/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
4. experiments/0824_EX06/04_conclusion.md
5. experiments/0824_EX07/{03_execution.md,04_conclusion.md}
6. experiments/0824_EX08/{03_execution.md,04_conclusion.md}
7. experiments/0825_EX01/04_conclusion.md
8. experiments/0826_EX01/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
9. experiments/0829_EX01/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
10. experiments/0829_EX02/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
11. experiments/0830_EX01/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
12. experiments/0830_EX02/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
13. experiments/0830_EX03/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
14. experiments/0830_EX04/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
15. experiments/0830_EX05/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
16. experiments/0830_EX06/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
17. experiments/0830_EX07/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
18. experiments/0830_EX08/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
19. experiments/0901_EX01/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
20. experiments/0901_EX02/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
21. experiments/0901_EX03/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
22. experiments/0901_EX04/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
23. experiments/0901_EX05/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
24. experiments/0901_EX06/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
25. experiments/0901_EX07/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
26. experiments/0901_EX08/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
27. experiments/0901_EX09/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
28. experiments/0901_EX10/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
29. experiments/0901_EX11/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
30. experiments/0901_EX12/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
31. experiments/0901_EX13/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
32. experiments/0901_EX14/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
33. experiments/0901_EX15/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
34. experiments/0901_EX16/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
35. experiments/0901_EX17/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
36. experiments/0901_EX18/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
37. experiments/0901_EX19/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
38. experiments/0901_EX20/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}
39. experiments/0901_EX21/{01_goal.md,02_design.md,03_execution.md,04_conclusion.md,experiment_manifest.json}

如果当前仓库没有.venv，先执行：
1. py -3.12 -m venv .venv
2. .\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows -e ".[test]"

再用当前设备执行：
1. .\.venv\Scripts\python.exe -m pip check
2. .\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
3. .\.venv\Scripts\czsc-trader.exe baseline validate --version baseline_20260826 --symbol 588080.SH
4. .\.venv\Scripts\czsc-trader.exe archive validate --all
5. .\.venv\Scripts\python.exe -m pytest -q

重要边界：当前身份只认configs/rule_baselines/registry.json；baseline_20260826是通用活动正式基线，与0824_EX04冻结策略同源，但调参与研究证据只来自588080；baseline_20260823只用于显式历史复现。研究事实只认Git跟踪的实验档案；引用某轮精确事实前必须完整阅读该轮档案。outputs只用于当前设备新运行的普通回测，隔离回放产物也不是研究证据。截至2026-08-31的数据已经被观察，不能再作为新的独立留出；新的未观察前向行情从2026-08-31之后开始。

默认在master做轻量开发；重量级开发或正式研究先询问是否新建分支；禁止git worktree。保护已有修改。只在任务需要时运行回测或研究，并使用命令实际返回的目录。正式研究先预注册，PASS、FAIL、ERROR和诊断结果都必须如实归档。实盘账户没有明确成交回报时一律按未成交处理。
```
