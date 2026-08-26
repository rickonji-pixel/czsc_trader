# 588080 CZSC策略研究：跨机新会话交接

> **最高优先级约束：始终按跨设备接力处理。**
>
> 不得假定新会话与上一会话位于同一台设备、同一绝对路径，或保留任何未提交文件。不得依赖历史 `outputs/`、本地SQLite数据库、虚拟环境和终端输出。研究事实只以Git跟踪的源码、配置、`data/raw/*_manifest.json`、`data/raw/*_validation.json` 和 `experiments/MMDD_EXX/` 为准。`outputs/`只用于普通回测输出，不是研究档案；需要结果时必须在当前设备重新运行，并使用命令实际返回的目录。

本文档记录截至 **2026-08-26 / 0826_EX01及基线晋升** 的可接力状态。新会话应先验证仓库当前状态，不能把本文中的分支或提交描述当成未经核验的实时事实。

## 1. 当前目标与两个基线

### 1.1 长期研究目标

- 标的固定为 `588080.SH`；
- 历史数据覆盖上市日至2026-08-25；
- 截至2026-08-25的数据已经被项目观察，全部属于已见历史证据；
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

### 4.1 如何读取历史证据

上表只负责给出研究路线、当前状态和档案索引，不替代正式实验记录。需要引用某轮实验的数值、判定规则或执行细节时：

1. 完整阅读该实验的`01_goal.md`、`02_design.md`、`03_execution.md`和`04_conclusion.md`；
2. 用`experiment_manifest.json`验证档案身份和文件哈希；
3. 从`artifacts/`读取机器可读证据，不从本文转抄历史数值；
4. 若历史档案中的基线身份与当前注册表不同，前者只表示实验当时状态，当前身份始终以`configs/rule_baselines/registry.json`为准。

研究链条可以概括为：0824_EX01—EX04建立并冻结EX04；0824_EX05—EX08扩大因子和搜索规模但未产生更优方案；0825_EX01对EX08候选补测仍全部失败；0825_EX02—EX06完成机制、路径、离场和新表示诊断；0826_EX01未找到跨年稳定的偏离边界，因此停止利用已见历史继续修补EX04。

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

默认`stdout`只输出一份JSON，进度和诊断进入`stderr`；`--format text`用于人工阅读。所有命令从当前目录向上发现仓库，也可显式传入`--repo-root`。

普通回测默认输出到 `outputs/<证券代码>_<MMDD>_RXX`，也可通过`--outputs-root`指定其他根目录；实际目录只认命令返回的`artifacts.output_dir`。未提供收益目标时只输出指标，验收状态为 `N/A`。

研究协议到实现的映射由`src/czsc_trader/research/registry.py`和各实验的`artifacts/protocol.json`共同决定。数值runner不提供独立CLI；跨机接力不得依赖本文维护易漂移的内部模块清单，实际代码位置应从当前提交和注册表发现。

行情适配器是位于`packages/dataflows/`的独立Python子项目，运行时导入名为`dataflows`且不得依赖`czsc_trader`。新设备使用`.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows -e ".[test]"`同时安装子项目和主项目；本地Tushare令牌只放在被Git忽略的仓库根目录`.env`中。安装后的CLI不得依赖仓库根目录出现在`PYTHONPATH`中。

## 6. 数据状态

588080受跟踪行情覆盖：

- 起点：2020-11-16（上市日）；
- 当前数据末日：2026-08-25；
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

远端默认使用SSH：

```powershell
git clone git@github.com:tomxiao/czsc_trader.git
cd czsc_trader
git switch master
git pull --ff-only origin master
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
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

测试集只保留统一CLI的端到端功能验证，覆盖行情校验、基线、普通回测、实验档案、冻结保护和隔离回放。

### 7.3 普通回测示例

```powershell
.\.venv\Scripts\czsc-trader.exe backtest run `
  --symbol 588080.SH --asset etf `
  --targets configs\backtest_targets\588080_2026.json
```

未指定`--baseline`时，588080默认使用`baseline_20260826`。上述2026目标文件只用于已见历史表现复算，不构成独立验收。必须读取命令返回的`artifacts.output_dir`，不得假设新设备存在任何历史输出目录。

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

1. 完整阅读本文和`configs/rule_baselines/registry.json`。
2. 按第7.2节完成Git、依赖、行情、基线、实验档案和端到端测试核验。
3. 完整阅读0824_EX04和0826_EX01的目标、设计、执行、结论及manifest，确认当前基线来源和停止回溯修补的依据。
4. 因子或搜索问题再补读0824_EX06—EX08和0825_EX01；归因或信号问题再补读0825_EX02—EX06。
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

如果当前仓库没有.venv，先执行：
1. py -3.12 -m venv .venv
2. .\.venv\Scripts\python.exe -m pip install -e ".[test]"

再用当前设备执行：
1. .\.venv\Scripts\python.exe -m pip check
2. .\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
3. .\.venv\Scripts\czsc-trader.exe baseline validate --version baseline_20260826 --symbol 588080.SH
4. .\.venv\Scripts\czsc-trader.exe archive validate --all
5. .\.venv\Scripts\python.exe -m pytest -q

重要边界：当前身份只认configs/rule_baselines/registry.json；baseline_20260826是588080活动正式基线，与0824_EX04冻结策略同源；baseline_20260823只用于显式历史复现。研究事实只认Git跟踪的实验档案；引用某轮精确事实前必须完整阅读该轮档案。outputs只用于当前设备新运行的普通回测，隔离回放产物也不是研究证据。2026已经被观察，不能再作为独立留出；新独立前向证据从2026-08-26之后开始。

默认在master做轻量开发；重量级开发或正式研究先询问是否新建分支；禁止git worktree。保护已有修改。只在任务需要时运行回测或研究，并使用命令实际返回的目录。正式研究先预注册，PASS、FAIL、ERROR和诊断结果都必须如实归档。实盘账户没有明确成交回报时一律按未成交处理。
```
