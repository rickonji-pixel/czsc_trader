# 588080 CZSC策略研究：跨机新会话交接

> **最高优先级约束：始终按跨设备接力处理。**
>
> 不得假定新会话与上一会话位于同一台设备、同一绝对路径，或保留任何未提交文件。不得依赖历史 `outputs/`、本地SQLite数据库、虚拟环境和终端输出。研究事实只以Git跟踪的源码、配置、`data/raw/*_manifest.json`、`data/raw/*_validation.json` 和 `experiments/MMDD_EXX/` 为准。`outputs/`只用于普通回测输出，不是研究档案；需要结果时必须在当前设备重新运行，并使用命令实际返回的目录。

本文档记录截至 **2026-08-24 / EX07** 的可接力状态。新会话应先验证仓库当前状态，不能把本文中的分支或提交描述当成未经核验的实时事实。

## 1. 当前目标与两个基线

### 1.1 长期研究目标

- 标的固定为 `588080.SH`；
- 历史数据覆盖上市日至2026-08-24；
- 每轮策略选择只使用不晚于2025-12-31的数据；
- 冻结策略后才允许读取2026，并在Q1、H1、M1-M8三个窗口验收；
- 当前挑战目标是三个窗口的收益率都严格高于研究基线；夏普率同时报告，但EX04之后的收益率单目标实验不以夏普判定PASS；
- 三个窗口必须全部成立，才算整体PASS。

2026不是项目永久不可见的数据。约束粒度是**单次实验**：同一实验内不得依赖2026改进策略；冻结后只做一次2026验收，验收结果不得反馈给本轮参数。启动下一轮实验时，可以知道此前实验结论，但必须重新预注册假设，并且只用2020—2025完成新策略选择。

### 1.2 通用回测基线

通用回测默认加载：

```text
configs/rule_baselines/baseline_20260823.json
```

实际默认版本由 `configs/rule_baselines/registry.json` 的 `latest` 决定。该文件是对任意A股股票/ETF执行固定规则回测时的正式基线，命名规则为 `baseline_YYYYMMDD.json`，不得原地修改。

### 1.3 当前研究基线

EX05—EX07使用以下策略作为公平研究基线：

```text
experiments/0824_EX04/artifacts/frozen_challenger.json
```

EX04是12因子四层架构的局部最优起点，但它没有在2026三个窗口全部超过历史冠军，因此**没有晋升为通用回测基线**。不得混淆：

| 名称 | 用途 | 当前身份 |
|---|---|---|
| `baseline_20260823` | 任意标的固定规则回测、历史冠军比较 | 注册表正式基线 |
| EX04冻结策略 | EX05以后研究挑战的比较对象 | 研究基线，不是注册表基线 |

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

### 3.2 EX06/EX07候选空间

- 候选总数：91项；
- 包括连续/类别因子以及低频事件状态；
- 事件型因子不再因低覆盖率或低激活天数被误删；
- 日线原始成交量因子强制保留；
- 至少保留一个EX04原始趋势因子；
- 非零因子数量限制为6—18；
- 每项非零绝对权重不低于0.0125；
- 原始趋势权重合计不低于0.10。

候选身份以 `experiments/0824_EX06/artifacts/factor_candidates.csv` 及其协议哈希为准。

## 4. EX01—EX07研究进度

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

## 5. 代码结构与入口

### 5.1 稳定用户入口

| 入口 | 职责 |
|---|---|
| `scripts/prepare_market_data.py` | 从dataflows/Tushare获取、后复权、验证并发布A股股票或ETF行情 |
| `scripts/run_backtest.py` | 对任意已准备证券执行冻结基线回测 |
| `scripts/run_experiment.py` | 运行早期预注册冠军挑战或EX02归因协议 |
| `scripts/run_holdout.py` | 对受跟踪且已冻结的挑战者执行独立2026检验 |

普通回测输出目录固定为 `outputs/<证券代码>_<MMDD>_RXX`。未提供收益目标时只输出指标，验收状态为 `N/A`。

### 5.2 研究实现模块

| 模块 | 职责 |
|---|---|
| `attribution.py` / `attribution_runner.py` | EX02冠军归因 |
| `four_layer.py` / `four_layer_runner.py` | EX03四层等价架构与权重阈值搜索 |
| `return_only_runner.py` | EX04收益率单目标搜索 |
| `factor_discovery.py` / `factor_discovery_runner.py` | EX05—EX06因子生成、事件支持和并行搜索 |
| `optuna_search.py` / `optuna_runner.py` | EX07参数投影、ask/evaluate/tell、SQLite恢复和TPE搜索 |
| `experiment_archive.py` | 实验目录、清单生成和哈希验证 |

EX03—EX07的运行器默认绑定各自实验目录。已有实验是冻结档案，不应为“复现”而直接覆盖运行。若要开展新实验，先创建新的 `MMDD_EXX` 目录并预注册协议，再显式传入 `--experiment-dir`。

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
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
git diff --check
```

完整测试耗时较长。日常开发按变更范围运行最小测试；合并研究框架或高风险改动时再扩大验证。不得把“未运行完整测试”等价表述为“完整测试通过”。

### 7.3 验证实验档案

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; from czsc_trader.experiment_archive import validate_experiment_archive; [validate_experiment_archive(p) for p in sorted(Path('experiments').iterdir()) if p.is_dir()]; print('experiment archives: PASS')"
```

该命令只验证Git档案完整性，不重新执行耗时研究。

### 7.4 普通回测示例

```powershell
.\.venv\Scripts\python.exe scripts\run_backtest.py `
  --symbol 588080.SH --asset etf `
  --baseline baseline_20260823 `
  --targets configs\backtest_targets\588080_2026.json
```

必须读取命令本次返回的 `output_dir`。不得假设新设备存在历史 `588080_0823_RXX` 目录。

## 8. Git与研究工作流

1. 默认在 `master` 做轻量修改和bugfix。
2. 重量级开发或正式研究先询问用户是否新建分支；禁止使用Git worktree。
3. 开工前检查并保护用户已有修改。
4. 实验目录按 `MMDD_EXX` 命名；日期变化后编号从EX01重新开始，同一天单调递增。
5. 每轮只研究一个可证伪假设，先冻结目标、数据边界、候选空间、排序和停止条件。
6. 2026解封后不得在同轮实验内二次调参。
7. PASS、FAIL、ERROR和诊断实验均须保留完整档案并纳入Git。
8. `outputs/`和运行态SQLite不得纳入研究提交。
9. 合并和推送后比较本地 `master` 与 `origin/master` SHA。

## 9. 下一轮研究建议

EX07证明：仅把贪心搜索替换为Optuna，并在固定91因子候选空间内联合搜索，没有突破EX04。下一轮不宜无条件增加Trial数量并重复同一目标。

建议先讨论并预注册一个新的EX08假设，优先方向为：

1. 诊断Trial 468在哪三个半年度窗口失效，定位是特定因子、因子交互还是阈值导致的最差窗口；
2. 保留四层架构和严格因果执行，允许根据2020—2025证据调整因子定义或候选构造；
3. 不用2026选择因子、搜索范围或停止点；
4. 仍以EX04作为公平研究基线；
5. 明确新实验是否继续使用maximin收益目标，避免搜索器被单个异常窗口完全支配。

这只是下一轮讨论起点，不是已批准协议。新会话不得直接启动EX08或访问2026调参，必须先与用户确认目标和实验设计。

## 10. 新会话接手清单

1. 完整阅读本文。
2. 确认仓库根目录、分支、远端和工作区状态。
3. 从 `registry.json` 区分通用回测基线与EX04研究基线。
4. 验证588080行情manifest和validation。
5. 验证EX01—EX07实验清单，不依赖历史outputs。
6. 阅读EX04、EX06、EX07的目标、设计和结论。
7. 修改前定位对应测试；修改后运行最小相关测试并明确测试范围。
8. 研究任务先预注册；普通回测只加载冻结规则。
9. 实盘账户状态与回测状态完全分离；没有明确成交回报时一律按未成交处理。
10. 所有结果明确标注样本选择期、冻结点、2026访问时点和PASS定义。

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

重要边界：baseline_20260823是通用回测正式基线；EX04冻结策略只是EX05以后使用的研究基线。EX07已完成608个Optuna Trial但没有新方案超过EX04，结论FAIL。研究事实只认Git跟踪的experiments档案；outputs只用于当前设备新运行的普通回测。2026约束按单次实验执行：策略冻结前不可访问，冻结后一次验收，不得把验收结果反馈给同轮参数。

默认在master做轻量开发；重量级研究先询问是否新建分支；禁止使用git worktree。保护所有已有修改。只在任务需要时运行回测或研究，并使用命令实际返回的目录。研究结果无论PASS还是FAIL都必须如实归档。实盘账户没有明确成交回报时一律按未成交处理。
```
