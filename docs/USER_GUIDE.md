# 用户使用说明

> 本文是CZSC Trader与CZSC PTE的统一用户手册，覆盖安装、数据、策略、回测、决策、
> 模拟交易和日常运维。策略研究流程见[研究交接](RESEARCH_HANDOFF.md)，系统架构与开发
> 规则见[技术交接](DEVELOPMENT_HANDOFF.md)。以下命令均在仓库根目录执行。

## 环境与安装

- Python 3.12；
- 发布新行情时需要Tushare Token，将`.env.example`复制为`.env`并填写
  `TUSHARE_TOKEN`；
- 使用Futu模拟交易时需要启动Futu OpenD；
- 只有安装、更新或删除WDG系统服务时需要Windows管理员权限。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_manager[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_evaluator[test]"
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
```

确认命令入口：

```powershell
.\.venv\Scripts\czsc-trader.exe --help
.\.venv\Scripts\pte.exe --help
Get-Command .\.venv\Scripts\pte-watchdog.exe
```

## 行情数据

### 研究数据集

`data/raw/`是受控研究池。准备或验证研究数据：

```powershell
.\.venv\Scripts\czsc-trader.exe data prepare `
  --symbol 588080.SH --asset etf `
  --start 2020-01-01 --end 2026-09-02

.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
```

研究必须在实验明确指定的数据边界内进行。查看研究数据、改变候选或选择策略版本时，
遵守[研究交接](RESEARCH_HANDOFF.md)中的数据污染规则。

### 普通回测数据集

`data/backtest/`独立维护，回测命令不会隐式更新数据：

```powershell
.\.venv\Scripts\czsc-trader.exe data update-backtest `
  --symbol 588080.SH --asset etf --through 2026-09-04
```

更新采用追加保护：既有30分钟线、日线和已结束周线不可改写；只有与新增日线处于同一
自然周的末根未完成周线可以重新聚合。校验或网络失败时保留原数据集。

策略统一使用Tushare后复权行情；交易委托、成交和估值使用未复权行情。发布器同时生成
30分钟、日线、周线及身份清单，并按交易日对齐信号价格和执行价格。

## 策略管理

当前正式策略身份为`S001 / 综合基线策略`。查看策略及证据：

```powershell
.\.venv\Scripts\czsc-trader.exe strategy list
.\.venv\Scripts\czsc-trader.exe strategy show --strategy S001 --version v1
.\.venv\Scripts\czsc-trader.exe strategy history --strategy S001
.\.venv\Scripts\czsc-trader.exe strategy performance --strategy S001 --version v1
.\.venv\Scripts\czsc-trader.exe strategy validate --all
```

`RESEARCH`表示版本仍可修改；`PAPER_READY`可以创建模拟账户；人工晋升到`LIVE_READY`
后才具备实盘部署资格；`RETIRED`禁止创建新运行实例。资格变化不会直接启停PTE。

旧命名基线只作为不可变历史依赖查看：

```powershell
.\.venv\Scripts\czsc-trader.exe baseline list
.\.venv\Scripts\czsc-trader.exe baseline show `
  --version baseline_20260903 --symbol 588080.SH
.\.venv\Scripts\czsc-trader.exe baseline validate `
  --version baseline_20260903 --symbol 588080.SH
```

`baseline_20260903`是`S001-v1`的只读历史别名。新策略版本统一登记在`strategies/`。
候选评估、冠军确认和冻结操作见[研究交接](RESEARCH_HANDOFF.md)。

## 回测

先显式更新普通回测数据集，再执行确定性回测：

```powershell
.\.venv\Scripts\czsc-trader.exe backtest run `
  --strategy S001 --strategy-version v1 --dataset backtest `
  --symbol 588080.SH --asset etf `
  --start 2026-01-05 --end 2026-09-04 --init-cash 100000
```

Backtest v2每次回放一个不可变策略快照、一个标的、一个明确日期区间。账户从指定现金和
零持仓开始；T日完整收盘决策在T+1执行。买入开盘价不高于限价时按开盘成交，盘中最低
价严格低于限价时按限价成交，相等触价保持未成交；卖出按下一交易日开盘成交。

`--symbol`可以指定与策略参考标的不同、但资产类型相同的实际回测标的，用于跨标的泛化
测试。该绑定只对本次回测生效，不修改策略注册、SM部署范围、PTE账户或`advice`行为。
报告和`manifest.json`会同时记录策略参考标的、实际回测标的及应用方式。

每次回测同时以独立的同额资金账户计算BuyHold和MA5/MA20参照，统一比较最大回撤、
卡玛比率、盈亏比、收益率和夏普率。参照策略不参与当前策略的SE审计或通过判定。

结果写入被Git忽略的`outputs/`，包括报告、账本、审计证据和交互图表。正式研究证据必须
归档到`experiments/YYYYMMDD_策略ID_EXnn/`；历史`MMDD_EXXX`目录继续有效。验证全部实验档案：

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all
```

## 生成交易决策

按实际持仓和可用现金生成`advice.v4`：

```powershell
.\.venv\Scripts\czsc-trader.exe advice run `
  --symbol 588080.SH --asset etf `
  --actual-quantity 0 --available-cash 100000 `
  --strategy S001 --strategy-version v1 `
  --format json
```

Trader负责计算限价、费用空间、目标仓位和100份整数倍委托数量。`advice run`不连接券商、
不提交订单、不修改账户。没有明确成交回报时，实际持仓保持不变。

## PTE模拟交易

PTE以虚拟账户为业务中心。每个虚拟账户绑定一个不可变策略发布和一个Futu渠道；一个
Futu渠道可以承载多个虚拟账户。渠道只负责执行、回报和对账，不绑定策略或生成决策。

运行前启动Futu OpenD，并确认存在唯一中国市场模拟账户。观测与干预页面为
<http://127.0.0.1:8080>。

默认运行规则：

- 订单和成交约每5秒对账，账户与持仓每60秒刷新；
- 每个交易日20:30后发布完整收盘数据，失败后退避重试；
- 发布成功后，每个运行账户只生成一次对应数据版本的决策；
- 新单只在有效交易日`09:30–11:30`、`13:00–14:57`提交；
- 暂停只阻止新订单，已有订单继续对账；撤单必须二次确认；
- 只有Futu明确返回的累计成交增量能够改变账户现金和持仓。

虚拟账户页的“订单意图”展示决策到Futu订单之间的状态。`待提交/提交中/已提交`属于正常
流转；“提交结果待确认”表示通信中断后无法确认券商是否受理，PTE会保留冻结资金、阻塞
该账户并同时查询Futu当前和历史订单。明确拒单会记录拒绝原因并释放未成交冻结资金。
控制台全局告警会汇总渠道阻塞、虚拟账户阻塞、待确认提交和数据发布失败。

### 虚拟账户

首次启动会幂等创建`s001-v1 / S001-v1模拟账户`，绑定`S001-v1`并分配10万元初始
资金。查看、创建、暂停和恢复账户：

```powershell
.\.venv\Scripts\pte.exe account list --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte.exe account create --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-v2 --name "S001-v2模拟账户" `
  --strategy S001 --strategy-version v2
.\.venv\Scripts\pte.exe account create --repo-root D:\CodeBase\czsc_trader `
  --account-id s002-v1 --name "S002-v1模拟账户" `
  --strategy S002 --strategy-version v1 `
  --symbol 510500.SH --asset etf --initial-cash 100000
.\.venv\Scripts\pte.exe account pause `
  --repo-root D:\CodeBase\czsc_trader --account-id s001-v2
.\.venv\Scripts\pte.exe account resume `
  --repo-root D:\CodeBase\czsc_trader --account-id s001-v2
```

本地活动订单在Futu当前及历史订单中缺失、无法归属的订单、订单关键字段不一致，或Futu
持仓与虚拟账户汇总持仓不一致时，渠道会阻止新单。
控制台的审计事件页可以按账户、策略、渠道和关联ID查询策略、交易、系统及其他事件。
页面统一显示北京时间。

### 前瞻观察与绩效

虚拟账户页的“前瞻观察”展示行情、CZSC笔、策略得分、信号、成交、目标持仓和实际持仓。
默认保留策略选择截止日前最后60个交易日作为背景，截止线右侧才属于该策略版本的前瞻
记录。查看图表不会改变数据身份；若使用新行情调参，新版本必须登记新的选择截止日。

登记模拟盘里程碑时，先由PTE导出证据，再由Trader写入策略注册表：

```powershell
.\.venv\Scripts\pte.exe performance export --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-v2 --recorded-by tomxiao `
  --start 2026-09-03 --end 2026-12-03 --output state\paper-forward.json
.\.venv\Scripts\czsc-trader.exe strategy evidence add --input state\paper-forward.json
```

日常净值保存在SQLite；只有人工复核、晋升或降级所需的里程碑证据进入Git。

## PTE与WDG启停

正式运行由WDG系统服务托管PTE。首次安装时，在管理员PowerShell中执行：

```powershell
.\.venv\Scripts\pte-watchdog.exe install-config --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte-watchdog.exe start --wait 30
```

系统服务名为`CZSC-PTE-Watchdog`，启动类型为自动。WDG每10秒检查PTE进程和8080
HTTP状态；连续3次失败后按5、30、60秒退避重启。

日常发布PTE代码或修改PTE业务配置后，只重启PTE，无需管理员权限：

```powershell
.\.venv\Scripts\pte.exe control restart `
  --repo-root D:\CodeBase\czsc_trader --wait 30
Invoke-RestMethod http://127.0.0.1:8080/api/status
```

PTE完成当前请求并正常退出，WDG随即拉起新实例。只有WDG自身升级、仓库路径或监听地址
变化时，才在管理员PowerShell中维护系统服务：

```powershell
Get-Service CZSC-PTE-Watchdog
Start-Service CZSC-PTE-Watchdog       # 同时启动PTE
Stop-Service CZSC-PTE-Watchdog        # 同时停止PTE
Restart-Service CZSC-PTE-Watchdog     # 同时重启WDG和PTE
.\.venv\Scripts\pte-watchdog.exe remove
```

开发调试时可直接运行`.\.venv\Scripts\pte.exe serve --repo-root .`，并通过`Ctrl+C`
结束。生产环境由WDG托管时不要再启动第二个`serve`实例。

## 状态、日志与故障检查

```powershell
Get-Service CZSC-PTE-Watchdog
Get-NetTCPConnection -LocalPort 8080 -ErrorAction SilentlyContinue
Invoke-RestMethod http://127.0.0.1:8080/api/status
Get-Content state\paper_trading\logs\watchdog.log -Tail 100
Get-Content state\paper_trading\logs\pte.log -Tail 100
```

端口冲突由PTE明确报错。恢复运行失败时先确认Futu OpenD可用，再检查渠道的账户、订单、
成交和持仓对账。数据发布告警结合交易日20:30后的审计事件和日志判断。

本机数据库、发布数据、图表缓存和日志位于`state/paper_trading/`且不进入Git。跨机延续
同一条模拟盘观察序列时，需要迁移完整运行目录，并重新核对Futu活动订单、成交和持仓。
