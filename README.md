# CZSC 固定规则研究与通用基线回测

跨设备基线、复现方式和新会话交接说明见 `docs/RESEARCH_HANDOFF.md`。`outputs/` 仅为本机生成目录，不随Git同步；复现时应使用受Git跟踪的目标配置运行冻结基线，再与 `docs/baselines/588080_2026_expected.json` 核对。

项目包含两条严格分离的流程：

- `run_research.py` 只研究 `588080.SH`，遍历预声明的23,760个候选并选择规则；
- `run_backtest.py` 对任意已准备的 A股股票或 ETF 应用一套冻结规则，不搜索或修改规则。

两条流程都通过 CZSC 1.0.1 生成多周期因子，并由 vectorbt 1.1.0 在下一交易日开盘执行和验证。最终仓位只能由 CZSC 因子状态机产生；收益、基准、日期或组合净值不得覆盖仓位。

当前绝对收益验收门槛为：2026Q1不低于10.5%、2026H1不低于82.5%、2026年1月至数据末日2026-08-21不低于60.0%。三项必须同时达到才是PASS；Buy & Hold、换手和交易反转诊断不参与PASS或候选排序。

## 588080 规则研究

```powershell
.\.venv\Scripts\python.exe scripts\run_research.py
```

结果写入 `outputs/<股票代码>_<MMDD>_RXX`，其中 `RXX` 自动使用当前设备上下一个未占用编号，
不会覆盖已有结果，也不要求不同设备使用相同编号。
其中 `report.md` 汇总2026Q1、2026H1和2026年1–8月的目标收益、策略收益和目标差额。三个周期分别
以100万元现金、零持仓建立独立 vectorbt 组合；首日开盘只执行上一交易日已形成的信号，
并分别输出 `orders_<周期>.csv` 与 `equity_<周期>.csv`。

每个周期同时生成 `chart_<周期>.html` 离线交互式日线图。主图包含日K、截至周期末由
CZSC 1.0.1 确认的笔、五笔/七笔背驰、因子仓位切换信号、周期初始因子对齐和 vectorbt 实际成交点；
副图展示结构、趋势、量价位置与综合因子分数。日期轴按实际K线交易日动态折叠周末、法定节假日和停牌日，避免无数据区间产生空白。五笔/七笔背驰同时进入结构因子。研究目录还输出 `trade_diagnostics.csv`，用于观察完整买卖回合、持仓天数和连续反转；诊断层不能改变仓位或选优结果。

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

第一轮实验只研究离场后的再入场冷却期和门控。研究入口在打开年度行情文件前强制截止到2025-12-31，2026数据不参与候选生成、评价或排序：

```powershell
.\.venv\Scripts\python.exe scripts\run_experiment.py
```

只有2023、2024、2025三个年度中，挑战者收益率和夏普率都严格高于 `baseline_20260823`，程序才输出 `frozen_challenger.json`。最大回撤、换手、持仓比例和交易次数只观察，不影响PASS和排序。

将通过者保存为受Git跟踪的冻结文件并提交后，才能在独立命令中读取2026：

```powershell
.\.venv\Scripts\python.exe scripts\run_holdout.py `
  --frozen-challenger configs\frozen_challengers\<文件名>.json
```

2026Q1、2026H1、2026M1-M8三个窗口必须各自同时满足收益率和夏普率严格高于冠军，才产生新冠军候选；程序不会自动晋升基线。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

默认命令运行快速离线测试并排除 `slow` 和 `network`。需要显式运行完整候选研究或实时 Tushare 测试时使用：

```powershell
.\.venv\Scripts\python.exe -m pytest -m slow -q
.\.venv\Scripts\python.exe -m pytest -m network -q
```

测试覆盖数据发布与哈希、CZSC因子截断不变性、固定规则选择、基线解析、订单因子溯源、vectorbt次日开盘成交、成本手算对账和最终审计产物。
