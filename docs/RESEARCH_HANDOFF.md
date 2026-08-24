# 588080 纯 CZSC 绝对收益目标策略：研究规则与会话交接

> **设备与输出约束（最高优先级）**
>
> 不得假定新会话与当前会话运行在同一台设备，不得假定仓库位于相同绝对路径，也不得假定任何历史 `outputs/` 目录、R编号或本地回测产物存在。`outputs/` 不受Git跟踪，不能作为跨会话交接依据。需要回测或研究结果时，必须先确认当前仓库根目录和受Git跟踪的数据、配置，再在当前设备重新运行相应命令，并仅使用该次命令实际返回的 `output_dir`。

## 0. 研究与通用回测边界

仓库现已增加独立的 A股股票/ETF 固定规则回测链路：

```text
prepare_market_data.py → PASS行情清单 → 最新冻结规则基线 → run_backtest.py
```

必须区分：

- `scripts/run_research.py`：仅用于588080候选研究、排序和选择，产生 `candidate_results.csv`、`selected_rule.json`；
- `scripts/run_backtest.py`：只加载一个已冻结基线，对指定标的执行，不遍历候选、不更新基线；
- `scripts/prepare_market_data.py`：通过 Tushare 获取 A股股票或 ETF 数据，在获取阶段统一生成后复权30分钟、日线和周线，验证后发布扁平年度CSV、manifest和validation报告。股票使用 `adj_factor`，ETF使用 `fund_adj`；OHLC乘因子、成交量除以因子、成交额不变，周线由后复权日线聚合。

首个受 Git 跟踪的规则基线是：

```text
configs/rule_baselines/baseline_20260823.json
```

基线统一命名为 `baseline_YYYYMMDD.json`，同一天只允许一个正式基线。`configs/rule_baselines/registry.json` 的 `latest` 决定通用回测的默认规则；每次回测还会把实际版本、完整规则和哈希写入本次 manifest。基线不能原地修改，研究达到 PASS 也不会自动晋升。

通用回测命令：

```powershell
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol 588080.SH --asset etf
```

未提供 `--targets` 时验收状态为 `N/A`，不套用588080研究目标。固定回测输出仍采用 `outputs/<证券代码>_<MMDD>_RXX`，但不包含候选排行和 `selected_rule.json`。

## 1. 必须遵守的研究目标和硬约束

### 研究目标

以 `588080.SH` 为唯一标的，分别验证：

- 2026Q1；
- 2026H1；
- 2026年1月到数据末日 2026-08-21。

绝对收益目标为：2026Q1不低于10.5%，2026H1不低于82.5%，2026M1-M8不低于60.0%。三项同时达到才是整体PASS。Buy & Hold继续报告，但不影响PASS或选优。若约束内无法达到目标，必须如实报告FAIL，禁止通过例外规则强行达标。

### 硬约束

1. 只能读取 `data/raw` 中的 30 分钟、日线和周线历史 K 线。
2. 所有仓位决策必须直接来自 CZSC 1.0.1 输出的结构或信号。
3. 因子日 `T` 只能读取不晚于 `T` 收盘的已完成数据，最早在下一交易日开盘成交。
4. 交易最小粒度为日，只做多或空仓，目标仓位只能是 `0` 或 `1`。
5. 三个周期分别以 1,000,000 元现金、零持仓启动；首日可以执行上一交易日已经形成且仍有效的 CZSC 因子状态。
6. 正式回测必须由 vectorbt 1.1.0 完成，并用独立现金/持仓账本复算。
7. 每笔订单必须有 `factor_event_id`，能够追溯到具体因子快照和仓位变化。
8. 禁止收益锁定、基准表现触发、季度强制持仓、特殊日期交易或任何非因子仓位覆盖。
9. 允许查看 2026 结果后调整 CZSC 信号、权重、阈值、入场/离场确认天数、最短持仓和入场门控，但必须标注样本内优化。
10. 轻量级修改和bugfix默认在 `master` 完成；重量级开发和研究使用本地Git分支。禁止使用 Git worktree。

更完整且受Git跟踪的有效资料见：

- `docs/superpowers/specs/2026-08-23-czsc-only-remediation-design.md`
- `docs/superpowers/plans/2026-08-23-czsc-only-remediation.md`
- `docs/baselines/588080_2026_expected.json`
- `configs/backtest_targets/588080_2026.json`

## 2. 最终策略如何计算

### 2.1 CZSC 原始信号

结构因子输入：

- 30分钟 `cxt_bi_status_V230101`；
- 30分钟 `cxt_third_buy_V230228`；
- 日线 `cxt_bi_status_V230101`；
- 周线 `cxt_bi_status_V230101`；
- 日线 `cxt_five_bi_V230619`；
- 日线 `cxt_seven_bi_V230620`。

趋势因子输入：

- 日线5、10、20日 `tas_ma_base_V221101`；
- 日线 `tas_macd_base_V221028`。

量价位置因子输入：

- 日线 `vol_window_V230731`；
- 日线 `pressure_support_V240406`。

### 2.2 信号映射

原始 CZSC 类别先映射为 `-1/0/+1`：

- 向上、多头、三买、底背驰、支撑位：`+1`；
- 向下、空头、顶背驰、压力位：`-1`；
- 其他、中性、未识别类别：`0`；
- 成交量排名 N7—N10：`+1`，N1—N3：`-1`，N4—N6：`0`。

每组内部等权平均，得到 `structure`、`trend` 和 `volume_position`，范围限制在 `[-1, 1]`。

### 2.3 最终固定规则

```text
factor_score = structure       × 0.30
             + trend           × 0.30
             + volume_position × 0.40
```

状态机参数：

| 参数 | 值 |
| --- | ---: |
| 入场阈值 | 0.15 |
| 离场阈值 | 0.00 |
| 入场连续确认 | 1天 |
| 离场连续确认 | 1天 |
| 最短持仓 | 3天 |
| 入场门控 | 无 |

空仓时，综合分数不低于0.15转为满仓；持仓至少3天后，综合分数不高于0.00转为空仓。当日目标变化在下一交易日开盘执行。

### 2.4 候选选择顺序

23,760个预声明候选先按目标仓位去重，再按以下顺序排序：

1. 三个绝对收益目标通过数量；
2. 三个周期中的最小目标差额；
3. 平均目标差额；
4. 更小最大回撤；
5. 更简单的规则；
6. 稳定规则ID。

换手和连续反转诊断不参与排序。

完整排行榜保存在 `candidate_results.csv`，最终规则保存在 `selected_rule.json`。

## 3. 无前视与订单溯源

### 信号和成交顺序

```text
T日收盘：生成CZSC信号、三个组因子、综合分数和目标仓位
T+1交易日开盘：vectorbt按目标仓位成交
T+1收盘：按收盘价计算当日组合净值
```

审计不仅要求 `signal_date < execution_date`，还要求执行日必须是信号日之后紧邻的下一条交易日期。

### 因子事件类型

- `Entry`：目标仓位从0变为1；
- `Exit`：目标仓位从1变为0；
- `InitialEntry`：独立周期从现金启动，但上一交易日的有效因子目标已经为1，因此在周期首日开盘对齐。

普通订单必须对应同日同方向的 `Entry` 或 `Exit`；周期首日买入对应 `InitialEntry`。审计还检查订单方向、事件日期、目标仓位变化和事件综合分数。

## 4. 代码结构

| 文件 | 职责 |
| --- | --- |
| `src/czsc_trader/data.py` | 只读加载原始CSV、校验字段/时间/OHLC、跨周期对账和SHA-256 |
| `src/czsc_trader/factors.py` | 调用CZSC、日末对齐原始信号、映射分数并生成三个组因子 |
| `src/czsc_trader/walk_forward.py` | 定义固定候选、因子状态机、候选排序和因子事件；文件名保留历史名称 |
| `src/czsc_trader/backtest.py` | vectorbt次日开盘回测、独立账本、周期独立启动和订单来源绑定 |
| `src/czsc_trader/audit.py` | 审计目标仓位、订单事件匹配及紧邻下一交易日执行 |
| `src/czsc_trader/charting.py` | 生成三张离线Plotly日线交互图 |
| `src/czsc_trader/research.py` | 编排完整研究、输出文件、报告、manifest和版本信息 |
| `scripts/run_research.py` | 588080候选研究入口 |
| `src/czsc_trader/baselines.py` | 校验、解析和显式晋升不可变规则基线 |
| `src/czsc_trader/market_data_prep.py` | 获取后规范化、跨周期验证、哈希和安全发布行情 |
| `src/czsc_trader/backtest_runner.py` | 编排任意标的固定基线回测和证据输出 |
| `scripts/prepare_market_data.py` | A股股票/ETF行情准备入口 |
| `scripts/run_backtest.py` | 任意已准备标的的固定规则回测入口 |
| `tests/` | 数据、因子截断、状态机、候选选择、成交、审计、图表和研究集成测试 |

`walk_forward.py` 中还保留未被正式管线调用的早期月度滚动函数，用于历史测试和对照。正式 `run_research` 只调用 `select_fixed_rule`，不得重新接入收益覆盖逻辑。

## 5. 单次运行产物说明

以下文件只会在当前设备实际运行相应命令后生成，不受Git跟踪。新会话不得预设它们已经存在；表中的文件名仅用于说明一次新运行返回的 `output_dir` 可能包含什么。

| 文件 | 含义 |
| --- | --- |
| `report.md` | 三周期结果、独立指标、审计摘要、规则和局限 |
| `metrics.json` | 机器可读的三周期指标、整体PASS和审计数据 |
| `manifest.json` | 数据截止日、原始数据哈希、固定规则、依赖版本和图表清单 |
| `selected_rule.json` | 最终固定规则参数 |
| `candidate_results.csv` | 目标仓位去重后候选的完整排序、目标及三周期表现 |
| `trade_diagnostics.csv` | 已完成买卖回合、持仓天数、费用和连续反转诊断 |
| `factors.csv` | 每日目标仓位、综合分数、阈值、三个组因子和全部原始CZSC信号 |
| `factor_events.csv` | 全历史入场、离场及周期初始对齐事件 |
| `orders_<周期>.csv` | vectorbt实际订单，含信号日、成交日、价格、费用和事件ID |
| `equity_<周期>.csv` | 每日收盘、目标/执行仓位、综合分数和组合净值 |
| `chart_<周期>.html` | 独立离线交互图，含日K、CZSC笔、背驰、因子事件和成交点 |

图表不绘制线段。五笔/七笔背驰在目标周期没有满足条件时不会显示标记，但相应检测层仍存在。

## 6. 环境、运行和验证

不得依赖任何设备上的历史绝对路径。进入仓库后先通过以下命令确认实际根目录：

```powershell
git rev-parse --show-toplevel
```

本仓库不依赖任何设备上的外部CZSC源码目录。直接从 `pyproject.toml` 安装 `czsc==1.0.1`；只有需要研究CZSC内部实现时才另行获取对应版本源码。

核心版本：

- Python 3.12；
- CZSC 1.0.1；
- vectorbt 1.1.0；
- pandas 3.0.5；
- numpy 2.5.2；
- Plotly 6.9.0。

安装或更新本项目：

```powershell
cd <仓库克隆目录>
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip check
```

默认快速测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
git diff --check
```

测试数量会随通用数据和回测覆盖增加；以每次命令输出的通过、跳过和取消选择数量为准，不再硬编码历史44项计数。

耗时的完整候选搜索和实时联网检查已分层：

```powershell
.\.venv\Scripts\python.exe -m pytest -m slow -q
.\.venv\Scripts\python.exe -m pytest -m network -q
```

执行候选研究（仅在任务明确要求重新研究时）：

```powershell
.\.venv\Scripts\python.exe scripts\run_research.py
```

输出目录自动使用 `outputs/<证券代码>_<MMDD>_RXX` 的下一个未占用版本，不覆盖当前设备已有目录。

脚本会在终端JSON中返回本次实际 `output_dir`。仅本次运行需要使用该目录时才记录返回值，不得硬编码任何历史R编号。

复现当前588080冻结基线（不执行候选搜索、不修改基线）：

```powershell
.\.venv\Scripts\python.exe scripts\run_backtest.py `
  --symbol 588080.SH --asset etf `
  --baseline baseline_20260823 `
  --targets configs\backtest_targets\588080_2026.json
```

### 新设备复现流程

新会话应始终按“新设备”处理：

```powershell
git clone git@github.com:tomxiao/czsc_trader.git
cd czsc_trader
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\run_backtest.py `
  --symbol 588080.SH --asset etf `
  --baseline baseline_20260823 `
  --targets configs\backtest_targets\588080_2026.json
```

然后执行以下核对：

1. 读取脚本返回的 `output_dir`，不要假设目录名；
2. 将其中 `baseline_rule.json` 与便携快照的 `baseline.rule` 比较，并核对注册表中的版本和规范化哈希；
3. 将 `metrics.json` 的 `acceptance_status`、`windows` 和 `audit` 与便携快照比较；
4. 将 `manifest.json` 的行情哈希、数据截止日、周期和依赖版本与便携快照比较；
5. 确认便携基线 `required_artifacts` 中的每个文件都已生成；
6. 浮点指标应按 `1e-12` 绝对容差核对，依赖版本不同造成差异时必须记录并重新验证，不能直接覆盖基线。

## 7. Git状态和开发流程

开始工作前：

```powershell
git switch master
git status --short
```

轻量级修改和bugfix默认直接使用 `master`。重量级开发或研究才创建本地分支，例如 `git switch -c research/<任务名称>`。禁止运行 `git worktree add`，也不要未经确认清理任何已有工作目录。

交付、提交和推送以效率优先，默认运行与变更直接相关的最小测试；高风险或研究逻辑变更再扩大验证范围。推送后比较本地与远端SHA。

## 8. 已知局限与风险

1. 基线规则主要来自单一ETF和有限历史，无法证明跨标的、跨市场或跨周期稳健性。
2. 规则由2026目标周期结果选择，存在样本内过拟合风险。
3. 候选空间较大，目标仓位去重和样本内选优仍不能消除过拟合风险。
4. 未识别CZSC类别共84次，统一按中性0分处理；增加映射前必须逐类验证含义。
5. 回测允许按资金比例持有小数份额，适合研究比较，不等同于真实交易中的整数份额约束。
6. 单边成本固定为0.05%，没有单独建立随流动性变化的滑点模型。
7. 任何本地研究或回测都不是投资建议或未来收益承诺。

## 9. 建议的后续工作

优先级从高到低：

1. 获取2026-08-21之后的新数据，冻结当前规则后进行真正样本外验证；新数据进入前不得再调整规则。
2. 增加整数份额、可配置滑点和成交额容量约束，并保留与当前研究口径的对照结果。
3. 对84次未知CZSC类别生成分类清单，确认语义后决定是否增加显式映射。
4. 在不查看目标结果的前提下扩展到其他ETF，检验规则迁移能力。
5. 如需重构，将 `walk_forward.py` 更名或拆分为固定规则选择模块；重构前必须建立当前规则、订单和指标的可复现回归基准。

不得把“继续提高既有样本内收益”作为首要后续目标。

## 10. 新会话接手检查清单

新会话开始后依次执行：

1. 阅读本文件和纯CZSC合规设计文档。
2. 用 `git rev-parse --show-toplevel` 确认当前仓库根目录，不假定任何绝对路径。
3. 运行 `git status --short`，保护用户已有修改。
4. 确认当前分支和 `master` 基线；轻量级修改可在 `master` 完成，重量级开发和研究使用本地分支。
5. 先阅读受Git跟踪的基线注册表、规则文件、行情manifest和validation；不要寻找或依赖历史 `outputs/`。
6. 修改前先写失败测试；修改后运行默认快速测试。只有修改候选研究逻辑时才额外运行 `-m slow`，实时数据权限检查使用 `-m network`。
7. 只有任务需要时才重新运行研究或回测，并使用命令本次返回的 `output_dir`；不得预设R编号。
8. 检查每笔订单的 `factor_event_id`，尤其关注周期首日 `InitialEntry`。
9. 确认没有重新引入收益、基准或日期触发的仓位覆盖。
10. 结果必须明确区分样本内优化和样本外验证。

## 11. 可直接复制给新会话的提示词

```text
请接手当前Git仓库中的588080纯CZSC多因子研究。不要假设仓库位于特定绝对路径；先运行 git rev-parse --show-toplevel 确认仓库根目录。

开始前完整阅读：
1. docs/RESEARCH_HANDOFF.md
2. docs/superpowers/specs/2026-08-23-czsc-only-remediation-design.md
3. configs/rule_baselines/registry.json 与其中登记的规则文件

当前有效基线是注册表登记的纯CZSC固定策略。不得假定任何历史outputs、R编号或本机产物存在；只有当前任务需要结果时才重新运行相应命令，并使用该次命令返回的actual output_dir。禁止重新引入收益锁定或任何非CZSC仓位覆盖。每笔订单必须对应factor_event_id，并在信号后的紧邻下一交易日开盘执行。

禁止使用git worktree，保护所有已有未提交修改。轻量级修改和bugfix默认使用master，重量级开发和研究使用本地分支。完成后默认运行与改动直接相关的最小测试；研究结果无论PASS或FAIL都必须如实报告，样本内优化不得声称是样本外验证。
```
