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

A股股票与ETF统一使用Tushare后复权（`hfq`）行情计算策略。发布器同时生成
并校验30分钟、日线和周线文件以及manifest、validation文件；另行发布带独立
manifest的未复权日线，仅用于生成可交易委托价格。策略价格与执行价格必须按
交易日期严格对齐，二者不得混用。

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
买入—卖出交易；只有盈利交易时显示“无亏损”，只有亏损交易时显示
“无盈利”，没有闭合交易时显示“无闭合交易”。BuyHold不合成人为期末卖出，
因此通常显示“无闭合交易”。机器结果通过`win_loss_ratio_status`保留相同语义。

同一比较表中的夏普率统一由独立净值序列计算：包含首个交易日相对于初始
资金的收益，按252个交易日年化，无风险收益率为0，并使用样本标准差。

均线交叉按每日收盘时的离散数值确认，不使用图表连线在两个交易日之间的
视觉交点：若交易日T收盘首次出现 `MA5 <= MA20`，T记为卖出信号日，并在
下一交易日T+1开盘卖出；图表买卖标记位于实际成交日，而不是信号日。

## 日频交易建议

```powershell
.\.venv\Scripts\czsc-trader.exe advice run `
  --symbol 588080.SH --asset etf `
  --actual-position 1 --quantity 50000
```

命令读取最新完整收盘数据，使用后复权行情计算活动信号，并使用同日未复权
收盘价和活动执行规则生成下一交易日建议。结果分别输出
`signal_reference_price`和`execution_reference_price`。`--actual-position`必须明确传入0或1；`--quantity`必须为正数且符合
100份整数倍。输出包含目标仓位、实际仓位、状态、动作、委托类型、买入最高
价或卖出操作说明，以及两个冻结版本的身份。

该入口不连接券商、不自动下单、不改写账户状态。未收到明确成交回报时，
继续使用原实际仓位再次运行。建议有效期为`NEXT_TRADING_SESSION`；盘中价格
只作执行风险观察，不改变最近完整收盘后的策略信号。

## 实验档案

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all
```

`experiments/MMDD_EXXX/`保存每轮实验的目标、设计、执行、结论和制品。
档案可校验但不再通过统一入口重新运行。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q -m archive
.\.venv\Scripts\python.exe -m ruff check src tests
```

默认测试包含单元、仓库契约及一条安装后CLI回测端到端链路；完整实验档案
校验使用显式的`archive`标记运行，避免拖慢日常开发。所有测试均不联网。
Ruff只属于开发测试依赖，不进入项目运行时依赖。

## 换行与身份哈希

仓库通过`.gitattributes`强制普通文本在所有平台使用LF，不受本机
`core.autocrlf`影响。`data/raw/*.csv`保持原始发布字节，常见二进制文件
明确按binary管理。

身份校验分为三类：策略与规则JSON使用语义哈希，忽略格式、键顺序和换行；
普通文本制品统一换行为LF后计算哈希；行情CSV和二进制制品计算原始字节
哈希。新增身份校验必须复用`czsc_trader.identity`，不直接对普通文本调用
`read_bytes()`计算SHA-256。
