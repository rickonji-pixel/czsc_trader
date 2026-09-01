# 研究交接

> 新会话和新设备不得假定存在相同的 `outputs/`、虚拟环境、SQLite文件或
> 其他本地运行态。交接结论只以Git跟踪的数据、配置、基线、实验档案和验证
> 结果为依据。

## 当前状态

- 活动基线：`baseline_20260901`
- 策略类型：`czsc_regime_weight`
- 晋升来源：`experiments/0901_EX20`候选143
- 基线注册表：`configs/rule_baselines/registry.json`
- 研究标的：`588080.SH`
- 研究数据：2020年至2025年
- 项目测试数据：2026年，随 `data/raw` 持续补充
- 当前行情截止：2026-09-01
- 已冻结实验档案：46个，截止 `0901_EX21`

本项目的研究边界是样本内策略研究。2026年用于项目测试和候选比较，但不
宣称独立样本外或实盘有效性；项目暂不引入实盘成交反馈。

## 当前冠军策略

`baseline_20260901`是从 `baseline_20260826`演化出的regime条件权重
策略。它保留相同的12个因子、计分阈值和状态机，使用滞后日线收盘价计算
60日效率比，将市场划分为trend、range和warmup，再选取对应的冻结权重。

关键身份：

- candidate_id：143
- strategy：`czsc_regime_weight`
- baseline SHA-256：
  `711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993`
- source：
  `experiments/0901_EX20/artifacts/frozen_challenger.json`
- source SHA-256：
  `6aa655c4d5ecf24e300eb6b4987f8ae1976ff2a6f3d52baf58b32e9dcb588377`

`baseline_20260826`和`baseline_20260823`已归档，但仍可显式解析。

### 2026年regime划分

以下划分直接由Git跟踪的 `588080_daily_*.csv`、冻结参数
`er_lookback=60`和`er_threshold=0.12654614652711713`重算，不依赖本地
`outputs/`。每个交易日先将收盘价滞后一天，因此当日开盘前已经可以确定
regime；`ER60`大于等于阈值为trend，低于阈值为range。trend表示价格路径
效率较高，既可能是上涨也可能是下跌，不等同于上涨趋势。

截至2026-09-01，按交易日连续性合并后的时间顺序如下：

| 顺序 | Regime | 起始日 | 结束日 | 交易日数 |
| ---: | --- | --- | --- | ---: |
| 1 | trend | 2026-01-05 | 2026-01-05 | 1 |
| 2 | range | 2026-01-06 | 2026-01-27 | 16 |
| 3 | trend | 2026-01-28 | 2026-01-29 | 2 |
| 4 | range | 2026-01-30 | 2026-02-24 | 12 |
| 5 | trend | 2026-02-25 | 2026-03-03 | 5 |
| 6 | range | 2026-03-04 | 2026-05-11 | 45 |
| 7 | trend | 2026-05-12 | 2026-06-08 | 20 |
| 8 | range | 2026-06-09 | 2026-06-10 | 2 |
| 9 | trend | 2026-06-11 | 2026-07-17 | 26 |
| 10 | range | 2026-07-20 | 2026-07-21 | 2 |
| 11 | trend | 2026-07-22 | 2026-07-22 | 1 |
| 12 | range | 2026-07-23 | 2026-09-01 | 29 |

合计161个交易日，其中trend为55日（34.16%），range为106日（65.84%）；
因计算2026年状态时已有此前历史数据，2026年没有warmup交易日。

## 数据状态

所有A股ETF数据均通过 `czsc-dataflows` 从Tushare获取，并统一采用后复权
（`hfq`）。`data prepare`发布30分钟、日线、周线、manifest和validation；
`data validate`只验证本地发布结果，不联网。

| 标的 | 名称 | 数据起始 | 数据截止 |
| --- | --- | --- | --- |
| 159352.SZ | 南方中证A500ETF | 2025-01-01 | 2026-09-01 |
| 159516.SZ | 国泰中证半导体材料设备主题ETF | 2025-01-01 | 2026-09-01 |
| 515050.SH | 华夏中证5G通信主题ETF | 2024-01-02 | 2026-09-01 |
| 588080.SH | 易方达上证科创板50成份ETF | 2020-01-01 | 2026-09-01 |

## 保留的项目结构

- `packages/dataflows/`：独立行情获取与后复权发布组件
- `data/raw/`：Git跟踪的正式行情数据
- `configs/backtest_windows/`：回测窗口
- `configs/rule_baselines/`：冻结基线与活动注册表
- `experiments/`：不可变研究档案
- `src/czsc_trader/`：数据、基线、回测和归档验证运行时
- `tests/test_cli_e2e.py`：唯一必要端到端测试
- `outputs/`：被忽略的普通回测输出

历史实验运行器、`experiment run/replay`入口和
`docs/superpowers/plans|specs`已退役。Git历史和实验档案继续保留研究
过程与结论；新研究如需代码，应按当轮目标重新引入最小实现。

## 新设备恢复

```powershell
git status --short --branch
git pull --ff-only origin master

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e packages\dataflows
.\.venv\Scripts\python.exe -m pip install -e .[test]
```

确认安装入口：

```powershell
.\.venv\Scripts\czsc-trader.exe --help
```

只应出现 `data`、`baseline`、`backtest`、`archive` 四类资源。

## 最小验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q

.\.venv\Scripts\czsc-trader.exe data validate --symbol 159352.SZ
.\.venv\Scripts\czsc-trader.exe data validate --symbol 159516.SZ
.\.venv\Scripts\czsc-trader.exe data validate --symbol 515050.SH
.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH

.\.venv\Scripts\czsc-trader.exe baseline validate `
  --version baseline_20260901 --symbol 588080.SH

.\.venv\Scripts\czsc-trader.exe archive validate --all
```

固定回测回归：

```powershell
.\.venv\Scripts\czsc-trader.exe backtest run `
  --symbol 588080.SH --asset etf `
  --start 2026-01-01 --end 2026-08-21
```

预期活动基线为 `baseline_20260901`，策略收益率为
`0.7528525916956634`。回测产生的 `outputs/`只用于本机检查，不纳入
交接或Git。

固定回测链路还会用相同初始资金、单边费率和交易窗口执行BuyHold与
MA5/MA20双均线策略。双均线仅使用当日及此前收盘价生成仓位，在下一交易
日开盘执行；窗口首日继承前一交易日已经形成的目标仓位。`report.md`统一
展示三种策略的最大回撤、卡玛比率、盈亏比、收益率和夏普率，并为每个
窗口生成独立的双均线日线图。盈亏比仅基于已闭合交易，缺少盈利或亏损
样本时为 `N/A`；BuyHold不合成期末卖出，因此该指标为 `N/A`。以上均为
运行时能力，不依赖或假定交接设备存在任何既有 `outputs/`文件。

## 后续研究约束

1. 先读取活动基线注册表，再定义挑战对象。
2. 每轮实验仍使用 `MMDD_EXXX`目录，并保存目标、设计、执行、结论和制品。
3. `outputs/`不能替代实验档案。
4. 不用未来数据反向改写同一实验的策略。
5. 数据更新后先验证三频、复权和manifest，再开展研究或回测。
6. 没有明确证据时，不宣称样本外、实盘或因果有效。
