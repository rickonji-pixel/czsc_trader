# CZSC 固定规则研究与通用基线回测

跨设备基线、复现方式和新会话交接说明见 `docs/RESEARCH_HANDOFF.md`。正式研究资料统一进入受Git跟踪的 `experiments/MMDD_EXX/`；`outputs/` 只保存普通固定规则回测的本机产物，不随Git同步。

项目只保留四个功能入口：

- `prepare_market_data.py`：获取、后复权并验证 A股股票或ETF行情；
- `run_backtest.py`：对任意已准备标的应用冻结规则，不搜索或修改规则；
- `run_experiment.py`：执行预注册研究并生成受Git跟踪的实验档案；
- `run_holdout.py`：只对已冻结挑战者执行不可见样本外检验。

回测和实验都通过 CZSC 1.0.1 生成多周期因子，并由 vectorbt 1.1.0 在下一交易日开盘执行和验证。最终仓位只能由 CZSC 因子状态机产生；收益、基准、日期或组合净值不得覆盖仓位。

当前绝对收益验收门槛为：2026Q1不低于10.5%、2026H1不低于82.5%、2026年1月至数据末日2026-08-21不低于60.0%。三项必须同时达到才是PASS；Buy & Hold、换手和交易反转诊断不参与PASS或候选排序。

## 数据准备与通用回测

先安装 Tushare 可选依赖并在被 Git 忽略的 `dataflows/.env` 配置 token：

```powershell
.\.venv\Scripts\python.exe -m pip install -r dataflows\requirements-dataflows.txt
```

准备 A股股票：

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_market_data `
  --symbol 600519.SH --asset stock `
  --start 2024-01-01 --end 2026-08-21
```

准备 A股 ETF：

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_market_data `
  --symbol 510300.SH --asset etf `
  --start 2024-01-01 --end 2026-08-21
```

数据获取阶段统一对 A股股票和 ETF 使用后复权：股票读取 `adj_factor`，ETF读取 `fund_adj`；OHLC乘复权因子、成交量除以复权因子、成交额保持实际金额，周线由后复权日线聚合。数据只有在复权因子完整、30分钟交易时段、每日8根K线、30分钟/日线和日线/周线对账全部通过后才会发布到 `data/raw`。manifest 会记录 `hfq`、因子来源和因子 SHA-256；回测只读取 PASS 清单并重新核对行情文件 SHA-256，不会隐式联网刷新数据。

默认使用 `configs/rule_baselines/registry.json` 登记的最新基线：

```powershell
.\.venv\Scripts\python.exe scripts\run_backtest.py `
  --symbol 600519.SH --asset stock
```

显式选择已冻结基线和指定区间：

```powershell
.\.venv\Scripts\python.exe scripts\run_backtest.py `
  --symbol 600519.SH --asset stock `
  --baseline baseline_20260823 `
  --start 2024-01-01 --end 2026-08-21
```

`baseline_20260823` 的完整规则和规范化哈希均受Git跟踪，便携验证快照为 `docs/baselines/588080_2026_expected.json`。基线统一命名为 `baseline_YYYYMMDD.json`，同一天只允许一个正式基线。研究不会自动晋升基线；基线文件登记后不可原地修改。未传 `--targets` 时，通用回测只报告指标，验收状态为 `N/A`。结果仍写入 `outputs/<证券代码>_<MMDD>_RXX`，但固定回测不会生成 `candidate_results.csv` 或 `selected_rule.json`。

复现588080当前冻结基线的三个验收周期：

```powershell
.\.venv\Scripts\python.exe scripts\run_backtest.py `
  --symbol 588080.SH --asset etf `
  --baseline baseline_20260823 `
  --targets configs\backtest_targets\588080_2026.json
```

只使用命令本次返回的 `output_dir`，并将其中的基线版本、规则哈希、行情哈希、区间指标、审计和必需产物与便携验证快照核对；不得查找或硬编码历史R编号。

## 小团队冠军—挑战者实验

第一轮正式研究档案是 `experiments/0824_EX01/`，其目标、设计、执行过程、结论和20个完整候选均可跨设备复核。该档案是本轮结论的唯一权威来源，不依赖任何本机 `outputs/` 目录。

第一轮实验只研究离场后的再入场冷却期和门控。研究入口在打开年度行情文件前强制截止到2025-12-31，2026数据不参与候选生成、评价或排序：

```powershell
.\.venv\Scripts\python.exe scripts\run_experiment.py
```

只有2023、2024、2025三个年度中，挑战者收益率和夏普率都严格高于 `baseline_20260823`，程序才输出 `frozen_challenger.json`。最大回撤、换手、持仓比例和交易次数只观察，不影响PASS和排序。

将通过者保存为受Git跟踪的冻结文件并提交后，才能在独立命令中读取2026：

```powershell
.\.venv\Scripts\python.exe scripts\run_holdout.py `
  --frozen-challenger experiments\<来源实验>\artifacts\frozen_challenger.json
```

2026Q1、2026H1、2026M1-M8三个窗口必须各自同时满足收益率和夏普率严格高于冠军，才产生新冠军候选；程序不会自动晋升基线。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

这是唯一测试命令。测试不执行实时联网请求或完整历史研究，覆盖数据发布与哈希、CZSC因子截断不变性、固定规则状态机、基线解析、订单因子溯源、vectorbt次日开盘成交、成本手算对账、实验隔离和档案完整性。
