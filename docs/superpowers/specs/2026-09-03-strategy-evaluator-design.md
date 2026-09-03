# Strategy Evaluator 设计

## 1. 背景

项目已经具备候选生成、回测、正式执行模拟、实验归档、Strategy Manager（SM）和
PTE。当前每个实验仍可自行决定指标、窗口和排名方式，容易让长期均值掩盖近期退化。
`0903_EX03`候选1010在多年窗口中排名靠前，但2026 YTD的收益、最大回撤、卡玛和交易
质量均低于`S001-v1`，说明项目需要统一的策略评估机制。

Strategy Evaluator（SE）统一回答：哪些候选有资格比较、谁在开发池中击败在位策略、
是否存在值得冻结的冠军，以及结论由哪些证据支持。

SE面向一个人的OPC团队设计。系统内部保持严谨，对操作者只暴露一个命令、一页摘要和
一次最终人工确认。

## 2. 目标与非目标

### 2.1 目标

- 统一新实验的候选评估标准；
- 固定四项核心指标、决策窗口和相对基线非劣边界；
- 使用无主观权重的最差窗口Pareto目标函数；
- 记录所有实际尝试候选，不只记录入围者；
- 自动完成筛选、正式排名和冻结前体检；
- 明确输出冻结、保留在位策略或证据不足；
- 只把人工接受的冠军交给SM；
- 保持历史实验归档不可变。

### 2.2 非目标

首版不建设独立服务、数据库、Web应用、多人审批、自动冻结、自动晋升、组合级资金
优化或通用统计研究平台。首版也不实现PBO、Model Confidence Set和自动候选生成。

## 3. 架构与依赖

SE采用同仓库独立Python包，只被Trader引用：

```text
研究实验
  -> CZSC Trader：数据、回测、执行模拟、标准化输入
  -> Strategy Evaluator：筛选、非劣、Pareto、冠军、体检结论
  -> CZSC Trader：实验归档、一页报告、人工确认入口
  -> Strategy Manager：冻结正式版本
  -> PTE：运行PAPER_READY版本
```

依赖方向固定为`czsc_trader -> strategy_evaluator`。SE不依赖Trader、SM、PTE、券商SDK
和研究运行时；SM与PTE也不引用SE。SE不读取仓库路径、不运行回测、不写文件，只接收
不可变数据并返回确定性结果。

## 4. 代码组织

```text
packages/strategy_evaluator/
├── pyproject.toml
├── README.md
├── src/strategy_evaluator/
│   ├── __init__.py
│   ├── models.py
│   ├── standards.py
│   ├── validation.py
│   ├── noninferiority.py
│   ├── pareto.py
│   ├── evaluator.py
│   └── reporting.py
└── tests/

src/czsc_trader/application/evaluation_service.py
tests/test_evaluation_service.py
```

独立包首版只依赖Python标准库。公开模型使用冻结dataclass和JSON兼容类型，不把Pandas
对象放入跨包契约。包本身不提供CLI。

## 5. 参赛范围

每轮比赛包含一个在位策略和一组同职责挑战者。

- 在位策略必须是不可变release，是所有相对分数的零点；
- 挑战者包含协议登记后实际生成的全部候选；
- 参赛者使用相同标的、资金、成本、数据截止日和执行规则；
- 同一优化实验中，非目标参数必须冻结；
- 所有候选进入试验台账，包括早期筛除和行为重复候选；
- 相同目标仓位序列只保留一个代表参加正式回测，全部计入尝试次数；
- Buy & Hold和MA只作报告参照，不进入版本排名；
- 不同职责或风险预算的策略默认分开比赛。

## 6. 输入模型

### 6.1 EvaluationProtocol

协议包含：

```text
schema_version
standard_version
experiment_id
research_objective
development_cutoff
incumbent_id
incumbent_hash
decision_windows
target_windows
execution_policy_hash
tightened_margins
shortlist_limit
target_requirements
candidate_manifest
```

首版`schema_version=1`、`standard_version=opc-v1`。协议必须在读取候选表现前落盘；实验
只能收紧统一边界。相同实验ID的协议不得覆盖。

`candidate_manifest`指向实验在评估前生成的标准候选清单。每项候选必须包含完整、可执行
的策略payload或指向实验内不可变payload的相对路径，Trader据此统一运行回测。实验专用
Python代码不能成为重现候选的唯一来源。

### 6.2 CandidateDescriptor

```text
candidate_id
candidate_hash
family
generation_stage
parent_candidate_id
behavior_hash
parameter_group
is_incumbent
```

每轮恰好一个`is_incumbent=true`。配置hash和行为hash用于审计、复现和行为去重。

### 6.3 MetricObservation

每条记录表示一个候选在一个窗口和执行场景中的表现：

```text
candidate_id
window_id
scenario_id
measurement_tier
net_cagr
total_return
max_drawdown
calmar
calmar_status
profit_factor
profit_factor_status
closed_trades
turnover
cost_drag
objective_values
```

`measurement_tier`为`SCREENING`、`FORMAL`或`STRESS`。`objective_values`保存本轮研究
目标特有的数值，例如Range收益、Range短亏次数或Trend捕获率；指标方向和最小实质改善
幅度由协议的`target_requirements`预先定义，不能在结果产生后增加。

指标状态固定为`VALID`、`LOW_SAMPLE`、`NO_CLOSED_TRADES`、`NO_WINS`、`NO_LOSSES`和
`UNAVAILABLE`。状态和值矛盾时输入无效；`NO_LOSSES`不等于无穷大；`LOW_SAMPLE`不能
证明候选优于在位策略。

### 6.4 TrialRecord

每个实际尝试候选必须有一条台账：

```text
candidate_id
candidate_hash
generation_stage
behavior_hash
screening_status
rejection_reason
```

台账数量用于报告搜索规模，并为未来DSR/PBO保留基础。

### 6.5 HealthEvidence

只对最终冠军候选提供：

```text
execution_audit_status
reproducibility_status
neighborhood_status
stress_status
trial_ledger_status
reason_codes
```

每项状态为`PASS`、`FAIL`或`INSUFFICIENT`。存在`FAIL`时不能建议冻结；存在关键
`INSUFFICIENT`时返回证据不足。

## 7. 输出模型

`EvaluationResult`固定包含：

```text
decision
incumbent_id
recommended_champion_id
candidate_count
unique_behavior_count
screened_count
formal_count
noninferiority_violations
pareto_front
worst_window
target_improvement
health_check
evidence_quality
reason_codes
```

`decision`只有：

- `RECOMMEND_FREEZE`：存在可提交人工确认的冠军；
- `KEEP_INCUMBENT`：挑战失败，继续使用在位策略；
- `INSUFFICIENT_EVIDENCE`：存在候选，但证据不足以冻结。

输入协议、在位身份或数据损坏属于运行错误，不生成业务决策。

## 8. OPC-v1标准

### 8.1 核心指标

日常决策只使用净CAGR、最大回撤、卡玛比率和Profit Factor。固定年度同时展示总收益。
盈亏比、胜率、每笔期望、Sharpe、Sortino、持仓时间、换手和成本是诊断字段，不进入
首版排名。

### 8.2 默认非劣边界

| 指标 | 默认边界 |
|---|---|
| 净CAGR | 基线为正时保留至少90%；基线非正时不得低于基线 |
| 最大回撤 | 恶化同时不超过2个百分点和15%相对幅度 |
| 卡玛 | 基线为正时保留至少90%；基线非正时候选必须为正 |
| Profit Factor | 汇总证据有效时保留至少85%，并且不得低于1 |

最大回撤取绝对边界和相对边界中更严格者。Profit Factor只在全开发期及拥有至少10笔
闭合交易的目标窗口执行非劣判断；交易不足的自然年度只作诊断。全开发期闭合交易少于
10笔时，候选不能得到冻结建议。

统一默认值只能通过新的`standard_version`修改，单个实验只能收紧。

### 8.3 决策窗口

首版强制一个全开发期窗口、每个数据完整的自然年度、截止日所在年度的YTD，以及至少
一个研究目标窗口。自然年度和YTD对净CAGR、最大回撤和卡玛执行非劣检验；目标窗口按
协议执行非劣和实质改善检验。

滚动窗口、状态细分和压力场景只对最终少量候选运行，控制OPC计算成本。长期窗口不得
覆盖关键年度或YTD的重大退化。

## 9. 评估算法

### 9.1 输入验证

评估前验证协议版本、唯一在位策略、候选与台账对应关系、窗口完整性、指标状态、执行
规则hash和边界收紧规则。在位策略数据不完整时整轮失败；单个挑战者运行失败时记录并
继续。

### 9.2 两阶段计算

```text
全部候选
  -> 快速统一回测
  -> 劣质筛选、行为去重、初步Pareto
  -> 预注册规则产生正式短名单
  -> 正式执行规则完整回测
  -> 非劣检验与最终排名
  -> 唯一推荐冠军进行体检
```

正式短名单包含在位策略、初步Pareto第一前沿和每个参数平台的代表。`shortlist_limit`是
计算成本目标；第一前沿超过目标时保留整个第一前沿并记录扩容原因。其余名额按确定性
目标函数和候选ID选择，禁止人工挑选。

### 9.3 相对分数与最差画像

对每个候选、窗口和核心指标计算方向统一的分数：

```text
relative_score = favorable_difference / allowed_deterioration
```

`0`表示相同，正数表示改善，`-1`表示到达非劣边界，小于`-1`表示违规。具体边界直接
使用8.2节公式，避免基线接近零时的不稳定相对除法。

每个候选对每项指标取所有适用决策窗口的最差分数：

```text
candidate_profile = (
    worst_net_cagr_score,
    worst_max_drawdown_score,
    worst_calmar_score,
    worst_profit_factor_score,
)
```

### 9.4 Pareto与冠军

通过非劣检验的候选在四项最差分数上做最大化Pareto排序。选择顺序为：

1. 非劣违规数量为0；
2. 达成本轮预注册目标的实质改善；
3. 最差画像Pareto层级；
4. 四项分数中位数；
5. 参数邻域通过；
6. 换手和成本更低；
7. 参数变化更小；
8. 候选ID只用于完全等价结果的稳定排序。

如果第一前沿没有候选能按预注册规则与在位策略明确区分，输出`INSUFFICIENT_EVIDENCE`。
系统允许没有冠军。

### 9.5 冻结前体检

只有一个预推荐冠军进入体检：正式执行重放与审计、重复运行复现、参数邻域、预注册
压力场景、试验台账和证据完整性。体检只检查已经选出的冠军，不允许继续调参。体检
使用开发池，不能表述为新的样本外证据。

## 10. 与SM和PTE的关系

候选和冠军均不是SM状态。失败候选只存在于实验归档。

SE输出`RECOMMEND_FREEZE`后，Trader展示一次人工确认。确认后Trader创建SM的
`RESEARCH`版本、写入`RESEARCH_BACKTEST`证据、校验配置、冻结为`PAPER_READY`，并
通过PTE现有控制边界注册独立虚拟账户。SE不自动调用这些流程，新版本也不改变旧版本
资格或停止旧账户。

冻结和账户注册采用可恢复编排：SM冻结一旦成功不会回滚；PTE暂时不可用时结果标记为
`PAPER_ACTIVATION_PENDING`，重复确认命令只重试账户注册，不重复创建版本。Trader和PTE
不通过Python包互相导入。

术语统一为：候选、在位策略、本轮冠军、可冻结冠军和模拟盘版本。每个实验最多推荐
一个可冻结冠军。

## 11. Trader接口

SE公开接口与两阶段计算保持一致：

```python
validate_protocol(protocol, standard=OPC_V1) -> None
screen_candidates(protocol, candidates, screening_observations, trials) -> ShortlistResult
rank_candidates(protocol, shortlist, formal_observations) -> RankingResult
finalize_evaluation(ranking, health) -> EvaluationResult
render_summary(result) -> str
```

Trader在一个命令内依次调用筛选、正式回测、排名、冠军体检和最终判定。SE各阶段均为
纯函数，Trader可以安全恢复中断的长时间回测。Trader提供唯一用户命令：

```powershell
czsc-trader strategy evaluate --experiment 0903_EXXX
```

人工接受评估使用一个高层命令，后续页面按钮调用同一应用服务：

```powershell
czsc-trader strategy accept-evaluation `
  --experiment 0903_EXXX `
  --actor tomxiao `
  --reason "确认冻结并进入模拟盘"
```

Trader读取协议、调用现有回测、组装输入、调用SE并原子写入实验产物。研究实验不能复制
SE算法。

## 12. 一页报告与产物

一页报告固定为：最终建议、比赛概况、四指标对比、冠军体检和主要理由。主要理由最多
三条支持和三条风险。

Trader写入：

```text
evaluation_protocol.json
trial_ledger.csv
screening_metrics.csv
screening_noninferiority.csv
screening_decisions.csv
formal_metrics.csv
noninferiority.csv
pareto_profiles.csv
health_check.json
evaluation_result.json
evaluation_report.md
```

`screening_decisions.csv`为每个挑战者记录行为去重、快速非劣淘汰、正式入围或名额截断，
并保存逐项失败原因；两类`noninferiority.csv`都必须包含`candidate_id`。最终结果保存三项
筛选审计文件的内容哈希，幂等读取时重新验证。详细产物进入实验`artifacts/`，本契约生效
前的历史实验不补写。

## 13. 失败与恢复

- 协议错误或在位策略缺失：命令失败，不创建部分业务结果；
- 单个候选失败：记录原因并继续；
- 所有挑战者失败：`KEEP_INCUMBENT`；
- 核心证据样本不足：`INSUFFICIENT_EVIDENCE`；
- 冠军体检失败：`KEEP_INCUMBENT`并保存失败证据；
- 输出失败：Trader使用临时目录并原子替换；
- 相同输入重复运行：必须得到相同结果；
- 已完成实验：禁止覆盖原结果。

## 14. 审计与统计边界

每次评估记录Git commit、协议hash、在位release hash、候选配置与行为hash、数据hash、
执行规则hash、全部候选数量、标准版本、SE版本和理由码。报告文字不参与决策hash。

首版采用预注册、完整试验台账、固定非劣边界、行为去重、多年度最差窗口、参数邻域、
执行压力和冻结后前瞻观察。DSR、PBO、区块Bootstrap和Model Confidence Set保留为后续
标准版本。单标的、低交易频率条件下，首版不输出缺乏可靠性的精确概率。

## 15. 测试策略

独立包覆盖：模型不可变性、JSON往返、边界只能收紧、正负和近零基线、最大回撤边界、
缺失与低样本语义、Pareto支配和并列、最差年度否决、行为去重、三种决策和稳定报告。

Trader集成覆盖：CLI协议入口、在位身份、执行hash、原子产物、确定性重复运行、唯一
生产依赖方向、人工接受的幂等冻结，以及PTE注册失败后的可恢复状态。SM/PTE均不引用
SE。

历史案例测试使用最小化`S001-v1`/候选1010夹具，验证加入2026 YTD后1010不能获得
`RECOMMEND_FREEZE`。测试不读取或修改历史实验归档。

## 16. 交付与迁移

1. 新建并安装`strategy_evaluator`；
2. 实现模型、标准、校验、非劣、Pareto、决策和报告；
3. Trader增加应用服务和`strategy evaluate`命令；
4. 提供示例协议和端到端夹具；
5. 更新根README、研究交接和开发交接；
6. 保持`0903_EX02`、`0903_EX03`及全部历史归档不变；
7. 后续Range实验通过统一评估器重新开始，不继续围绕1010调参。

最终OPC操作保持为：

```text
提出研究问题 -> 运行一次评估 -> 阅读一页报告 -> 决定是否冻结
```
