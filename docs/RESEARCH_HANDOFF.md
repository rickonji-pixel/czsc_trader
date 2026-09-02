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
- 已冻结实验档案：48个，截止 `0902_EX02`

当前活动基线的选择样本截止到2026-08-28。2026年历史数据已经参与候选比较，
因此不能整体视为独立样本外。模拟交易从2026-09-01收盘信号、2026-09-02首次
执行开始形成前瞻运行记录；这部分记录用于观察策略、执行和系统可靠性，在积累出
足够样本前不宣称样本外或实盘有效性。项目尚未引入真实资金成交反馈。

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
  `711254af3fe951cc0eb32c81121ef52233a0cf2577f46b14683b2f3e6b961993`

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

所有A股ETF数据均通过 `czsc-dataflows` 从Tushare获取。策略行情统一采用后复权
（`hfq`）；委托定价使用同一来源、同一交易日的未复权日线，并通过独立
execution manifest校验。`data prepare`同时发布两种价格口径；`data validate`
只验证本地策略行情，`advice run`还会强制验证未复权执行价格，不联网。

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
- `packages/paper_trading_engine/`：独立模拟交易、对账、观测和干预运行时
- `docs/superpowers/specs/`：正式设计文档
- `docs/superpowers/plans/`：正式实现计划
- `tests/test_cli_e2e.py`：唯一必要端到端测试
- `outputs/`：被忽略的普通回测输出

通用历史实验运行器和`experiment run/replay`入口已退役。Git历史、正式设计、
实现计划和实验档案共同保留研究过程与结论；新研究按当轮目标引入最小实现。

## 低关注度执行规则研究

`0902_EX02`以活动基线`baseline_20260901`的目标仓位为固定输入，研究提前
提交、当日有效的限价入场规则。2020—2025用于选择，候选先满足T+1入场
周期成交率不低于90%，再最小化最高买入价。唯一冻结候选为固定比例0%，
即最高买入价取T日收盘价并向下对齐到0.001元。

研究集共有64个入场周期，T+1成交率93.75%，两日内与最终成交率均为
100%；2026截至9月1日共有7个周期，T+1成交率85.71%，两日内与最终
成交率均为100%。2026冻结执行规则收益率65.48%、最大回撤-12.09%、
夏普率2.4905；无条件次日开盘执行分别为70.94%、-12.09%、3.1508。

2026差异集中在首次入场：2025-12-31信号对应的1月5日限价未成交，1月6日
成交；1月5日开盘至收盘上涨3.47%，实际成交价相对首个执行日开盘高3.41%。
冻结规则已经完成因果和身份审计，并已晋升为
`execution_policy_20260902`。活动注册表位于`configs/execution_policies/`，
且绑定`588080.SH`、`baseline_20260901`及其SHA-256；正式研究机器证据仍以
`experiments/0902_EX02/`为准。

## 执行规则运行入口

日频建议命令：

```powershell
.\.venv\Scripts\czsc-trader.exe advice run `
  --symbol 588080.SH --asset etf `
  --actual-quantity 0 --available-cash 1000000 `
  --format json
```

该命令输出`advice.v2`，只读取最新完整收盘数据，实际持仓和可用现金必须显式
输入。买入数量由项目侧按冻结限价、单边费率和100份交易单位计算，尽可能使用
全部可部署现金；离场目标为卖出全部已成交持仓。未收到明确成交回报时，实际
持仓保持不变。运行时不读取或改写账户账本，也不连接券商。

588080固定回测现在展示四种口径：活动基线·次日开盘、活动基线·执行规则、
BuyHold和MA5/MA20。其他标的在没有匹配执行规则时继续展示原三种口径。

## 模拟交易观察

PTE通过CLI调用上述`advice.v2`，负责模拟账户对账、订单提交、成交增量记录、
SQLite审计和本机观测页面。当前渠道为Futu模拟交易，PTE与Trader是同仓库并列包；
PTE不导入`czsc_trader.*`，渠道不参与策略计算、定价或改量。

模拟运行记录位于被Git忽略的`state/paper_trading/`。跨机恢复时不得把另一台机器
的SQLite、日志、账户余额或订单状态当作已同步事实；新机器必须重新连接渠道并完成
账户对账。若需要延续同一观察序列，应另行安全迁移运行库并核对渠道订单，不能仅靠
Git恢复。

模拟交易观察不会反向修改冻结基线或执行规则。后续研究引用模拟结果时，必须区分
信号、订单意图、渠道受理和明确成交回报；没有成交回报时按未成交处理。

## 新设备恢复

```powershell
git status --short --branch
git pull --ff-only origin master

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e packages\dataflows
.\.venv\Scripts\python.exe -m pip install -e .[test]
```

如需同时恢复模拟交易开发或运行环境，再安装PTE：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
```

确认安装入口：

```powershell
.\.venv\Scripts\czsc-trader.exe --help
```

只应出现 `data`、`baseline`、`backtest`、`advice`、`archive` 五类资源。

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

固定回测链路还会用相同初始资金、单边费率和交易窗口执行活动执行规则、
BuyHold与MA5/MA20双均线策略。执行规则仅在标的与活动基线身份匹配时加入；
双均线仅使用当日及此前收盘价生成仓位，在下一交易
日开盘执行；窗口首日继承前一交易日已经形成的目标仓位。`report.md`统一
展示各策略的最大回撤、卡玛比率、盈亏比、收益率和夏普率，并为每个
窗口生成独立的双均线日线图。盈亏比仅基于已闭合交易，缺少盈利或亏损
样本时为 `N/A`；BuyHold不合成期末卖出，因此该指标为 `N/A`。以上均为
运行时能力，不依赖或假定交接设备存在任何既有 `outputs/`文件。

双均线交叉按交易日收盘时的离散MA数值确认，不把图表连线在相邻交易日
之间的视觉交点当作信号。T日收盘确认交叉后，订单在T+1交易日开盘执行，
图表买卖标记显示在实际成交日。

## 后续研究约束

1. 先读取活动基线注册表，再定义挑战对象。
2. 每轮实验仍使用 `MMDD_EXXX`目录，并保存目标、设计、执行、结论和制品。
3. `outputs/`不能替代实验档案。
4. 不用未来数据反向改写同一实验的策略。
5. 数据更新后先验证三频、复权和manifest，再开展研究或回测。
6. 没有明确证据时，不宣称样本外、实盘或因果有效。
7. 新实验先读取活动基线与活动执行规则注册表，再使用当日尚未占用的下一个
   `MMDD_EXXX`编号；当前最后一个冻结档案为`0902_EX02`。
8. PTE运行库和页面只作为前瞻观察材料，不替代`experiments/`中的研究设计、
   机器证据与结论归档。

## 可移植身份约束

- 普通文本统一使用LF；不得新增单文件CRLF例外；
- JSON规则和JSON来源制品使用语义SHA-256；
- CSV、Markdown、Python等普通文本先归一化为LF再计算SHA-256；
- `data/raw/*.csv`与二进制文件使用原始字节SHA-256；
- 所有新实现复用`src/czsc_trader/identity.py`，避免在业务模块内重新实现哈希；
- 历史实验档案保持原样，档案验证继续忽略换行差异及运行态字节码缓存。
