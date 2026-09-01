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
