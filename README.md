# czsc-trader

面向A股股票与ETF的CZSC策略研究、固定基线回测和数据验证工具。

## 环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e packages\dataflows
.\.venv\Scripts\python.exe -m pip install -e .[test]
```

根包依赖本仓库的 `czsc-dataflows`，行情发布时从 `.env` 读取
Tushare凭据。普通验证和回测不会隐式联网。

## 正式入口

```powershell
.\.venv\Scripts\czsc-trader.exe data prepare --help
.\.venv\Scripts\czsc-trader.exe data validate --help
.\.venv\Scripts\czsc-trader.exe baseline list --help
.\.venv\Scripts\czsc-trader.exe baseline show --help
.\.venv\Scripts\czsc-trader.exe baseline validate --help
.\.venv\Scripts\czsc-trader.exe backtest run --help
.\.venv\Scripts\czsc-trader.exe advice run --help
.\.venv\Scripts\czsc-trader.exe archive validate --help
```

CLI只保留五类资源：`data`、`baseline`、`backtest`、`advice`、`archive`。
历史实验执行与重放已退役；`experiments/`只保存不可变研究档案。

## 数据

```powershell
.\.venv\Scripts\czsc-trader.exe data prepare `
  --symbol 588080.SH --asset etf `
  --start 2020-01-01 --end 2026-09-01

.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
```

A股股票与ETF统一使用Tushare后复权（`hfq`）行情。发布器同时生成并校验
30分钟、日线和周线文件以及manifest、validation文件。

当前Git跟踪数据：

| 标的 | 名称 | 起始日 | 截止日 |
| --- | --- | --- | --- |
| 159352.SZ | 南方中证A500ETF | 2025-01-01 | 2026-09-01 |
| 159516.SZ | 国泰中证半导体材料设备主题ETF | 2025-01-01 | 2026-09-01 |
| 515050.SH | 华夏中证5G通信主题ETF | 2024-01-02 | 2026-09-01 |
| 588080.SH | 易方达上证科创板50成份ETF | 2020-01-01 | 2026-09-01 |

## 活动基线

活动基线由 `configs/rule_baselines/registry.json` 决定。当前为
`baseline_20260901`，策略类型 `czsc_regime_weight`，来源候选143。

```powershell
.\.venv\Scripts\czsc-trader.exe baseline show `
  --version baseline_20260901 --symbol 588080.SH

.\.venv\Scripts\czsc-trader.exe baseline validate `
  --version baseline_20260901 --symbol 588080.SH
```

未显式指定基线的回测默认使用注册表中的活动基线。

## 回测

```powershell
.\.venv\Scripts\czsc-trader.exe backtest run `
  --symbol 588080.SH --asset etf `
  --start 2026-01-01 --end 2026-08-21
```

输出目录使用 `证券代码_MMDD_BTXX` 格式并写入 `outputs/`。该目录被
Git忽略，只用于普通回测结果，不作为研究交接依据。

每次回测会在相同现金、费率和窗口下执行活动基线的次日开盘理论口径、
BuyHold与MA5/MA20双均线策略。若活动执行规则与标的、活动基线身份完全
匹配，还会增加“活动基线·执行规则”一行，并输出`execution_orders*.csv`
和`execution_equity*.csv`。双均线在日线收盘后计算，`MA5 > MA20`时目标仓位为
100%，否则为空仓，并在下一交易日开盘执行。`report.md`按窗口逐行比较
各策略的最大回撤、卡玛比率、盈亏比、收益率和夏普率；同时生成原有
活动基线图及独立的 `ma_chart*.html`双均线日线图。盈亏比只统计已闭合的
买入—卖出交易；若没有同时出现盈利与亏损交易则显示 `N/A`，BuyHold因
不合成人为期末卖出，盈亏比固定为 `N/A`。

均线交叉按每日收盘时的离散数值确认，不使用图表连线在两个交易日之间的
视觉交点：若交易日T收盘首次出现 `MA5 <= MA20`，T记为卖出信号日，并在
下一交易日T+1开盘卖出；图表买卖标记位于实际成交日，而不是信号日。

## 日频交易建议

```powershell
.\.venv\Scripts\czsc-trader.exe advice run `
  --symbol 588080.SH --asset etf `
  --actual-position 1 --quantity 50000
```

命令读取最新完整收盘数据，使用活动信号基线和活动执行规则生成下一交易日
建议。`--actual-position`必须明确传入0或1；`--quantity`必须为正数且符合
100份整数倍。输出包含目标仓位、实际仓位、状态、动作、委托类型、买入最高
价或卖出操作说明，以及两个冻结版本的身份。

该入口不连接券商、不自动下单、不改写账户状态。未收到明确成交回报时，
继续使用原实际仓位再次运行。建议有效期为`NEXT_TRADING_SESSION`；盘中价格
只作执行风险观察，不改变最近完整收盘后的策略信号。

## 跨机新会话交接

新会话先阅读本README，并按需阅读[研究交接](docs/RESEARCH_HANDOFF.md)。
仓库中的代码、行情清单、活动基线、活动执行规则和实验档案构成可移植的
Git事实；实际账户、人工选择和成交回报保存在本地`state/<证券代码>/`账本中。

### 1. 同步Git事实

源机器记录并推送当前提交：

```powershell
git status --short --branch
git rev-parse HEAD
git remote get-url origin
git push origin master
```

目标机器同步后核对相同提交：

```powershell
git pull --ff-only
git status --short --branch
git rev-parse HEAD
```

随后按“环境”一节安装依赖，并验证正式资源：

```powershell
.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
$baselineRegistry = Get-Content configs\rule_baselines\registry.json | ConvertFrom-Json
$activeBaseline = $baselineRegistry.latest
.\.venv\Scripts\czsc-trader.exe baseline show `
  --version $activeBaseline --symbol 588080.SH
.\.venv\Scripts\czsc-trader.exe baseline validate `
  --version $activeBaseline --symbol 588080.SH
Get-Content configs\execution_policies\registry.json
.\.venv\Scripts\czsc-trader.exe archive validate --all
```

活动基线和活动执行规则均以各自的`registry.json`为准。正式建议输出会同时给出
两个冻结版本及其SHA-256，可用于确认信号规则和执行规则来自同一组合。

### 2. 安全迁移本地账户账本

`state/`包含账户和成交信息，通过用户认可的安全渠道单独复制。源机器与目标
机器分别运行以下命令，逐文件比较SHA-256：

```powershell
Get-ChildItem state\588080 -File | Get-FileHash -Algorithm SHA256
```

目标机器需要继续把账本排除在Git之外：

```powershell
$excludePath = ".git\info\exclude"
if (-not (Select-String -LiteralPath $excludePath -Pattern '^/state/588080/$' -Quiet)) {
  Add-Content -LiteralPath $excludePath -Value "/state/588080/"
}
git check-ignore -v state/588080/account.json `
  state/588080/decisions.csv state/588080/fills.csv
```

三个文件的职责保持独立：

- `account.json`：当前实际持仓、交易资金和账户修订号；
- `decisions.csv`：日频建议与用户的`EXECUTE`、`PARTIAL`、`SKIP`选择；
- `fills.csv`：用户明确确认的成交及追加式更正记录。

成交回报至少包含交易日期、买卖方向、仓位类别、100份整数手数量和成交价。
手续费未明确时记录毛额并标记待确认；收到手续费后再确认可用于恢复交易仓的
净资金。成交价格更正通过`correction_of`追加新事件，原事件保留在审计链中。

### 3. 从账本映射建议参数

`advice run`保持无状态，因此新会话每次都从`account.json`读取实际账户，再显式
传入参数。对于“核心仓长期持有、交易仓由基线控制”的分仓账户：

- `--actual-position`表示交易仓状态：持有为`1`，空仓为`0`；
- `--quantity`表示本次策略允许操作的交易仓份数，使用100份整数倍；
- 核心仓不计入`--quantity`，并在最终决策单中单独说明；
- 恢复交易仓时，数量同时受历史交易仓上限和已确认卖出净资金约束；
- 手续费或净资金待确认期间，账户资金约束优先，恢复动作保持等待。

示例：交易仓当前持有15,000份时运行：

```powershell
.\.venv\Scripts\czsc-trader.exe advice run `
  --symbol 588080.SH --asset etf `
  --actual-position 1 --quantity 15000
```

正式决策单还应补充数据截止日及验证状态、上一日和当日目标仓位、是否出现新
信号、三个聚合因子、总分与阈值、核心仓和交易仓范围、信号新鲜度、开盘跳空
及数据异常风险、理论与实际仓位差异，并明确建议的人工辅助属性。

### 4. 新会话接手提示词

以下内容可以直接提交给新的Codex会话：

```text
请在czsc-trader仓库内工作。先完整阅读README.md，并按需阅读
docs/RESEARCH_HANDOFF.md。只读核对当前分支、HEAD、数据manifest和validation、
活动信号基线registry、活动执行规则registry，以及state/588080下的account.json、
decisions.csv和fills.csv。

日频建议使用安装后的czsc-trader advice run正式入口。实际仓位和数量从账户
账本显式传入；核心仓与交易仓分别说明。完整收盘数据生成下一交易日信号，
盘中价格只用于执行风险观察。程序保持人工辅助，不连接券商。

只有我提供明确成交回报后才能追加fills.csv并更新account.json。成交更正保留
原事件并追加correction_of记录。未收到成交回报时，账户持仓和修订号保持不变。
state/588080继续只保存在本地并排除在Git之外。
```

## 实验档案

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all
```

`experiments/MMDD_EXXX/`保存每轮实验的目标、设计、执行、结论和制品。
档案可校验但不再通过统一入口重新运行。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q
```

项目只保留一个不联网的端到端测试文件，验证安装后的CLI、数据、活动基线、
固定回测和全部实验档案。

## 换行与身份哈希

仓库通过`.gitattributes`强制普通文本在所有平台使用LF，不受本机
`core.autocrlf`影响。`data/raw/*.csv`保持原始发布字节，常见二进制文件
明确按binary管理。

身份校验分为三类：策略与规则JSON使用语义哈希，忽略格式、键顺序和换行；
普通文本制品统一换行为LF后计算哈希；行情CSV和二进制制品计算原始字节
哈希。新增身份校验必须复用`czsc_trader.identity`，不直接对普通文本调用
`read_bytes()`计算SHA-256。
