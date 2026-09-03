# CZSC Trader / CZSC PTE

本仓库包含两个面向用户的并列运行包，以及两个由 Trader 隐藏调用的领域包：

| 包 | 目录 | 命令 | 职责 |
| --- | --- | --- | --- |
| CZSC Trader | `src/czsc_trader/` | `czsc-trader` | 行情验证、策略管理、回测、研究归档和交易决策 |
| CZSC PTE | `packages/paper_trading_engine/` | `pte`、`pte-watchdog` | 模拟账户对账、自动下单、审计、观测页面和进程保活 |
| Strategy Manager | `packages/strategy_manager/` | 通过`czsc-trader strategy`使用 | 策略身份、不可变版本、资格、审计和绩效证据 |
| Strategy Evaluator | `packages/strategy_evaluator/` | 由Trader内部调用 | 候选筛选、Pareto排名、完整冠军审计和冻结建议 |

`packages/dataflows/`是行情获取与发布依赖。Trader 通过
`advice.v4` JSON 契约向 PTE 提供决策；PTE 不导入 Trader 或 Strategy Manager，券商渠道
不参与策略计算、定价或改量。

候选评估使用`opc-v3`时，完整体检均由Strategy Evaluator判定；Trader负责行情、回测、
事实证据和归档。统计风险标签供人工冻结决策使用，不自动改变SM或PTE状态。

研究现状见[研究交接](docs/RESEARCH_HANDOFF.md)，跨机开发与运行恢复见
[开发交接](docs/DEVELOPMENT_HANDOFF.md)。

## 环境要求

- Python 3.12
- Tushare Token：仅在发布新行情时需要，将`.env.example`复制为`.env`并填写
  `TUSHARE_TOKEN`
- Futu OpenD：仅运行当前 Futu 模拟交易渠道时需要
- Windows 管理员权限：仅安装、更新或删除 watchdog 系统服务时需要

普通数据验证、回测和 advice 读取本地数据，不隐式联网。

## 安装

在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_manager[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_evaluator[test]"
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
```

确认三个命令入口均可用：

```powershell
.\.venv\Scripts\czsc-trader.exe --help
.\.venv\Scripts\pte.exe --help
Get-Command .\.venv\Scripts\pte-watchdog.exe
```

## CZSC Trader 使用

### 数据准备与验证

```powershell
.\.venv\Scripts\czsc-trader.exe data prepare `
  --symbol 588080.SH --asset etf `
  --start 2020-01-01 --end 2026-09-01

.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
```

策略统一使用 Tushare 后复权行情；交易委托价格使用同日未复权日线。发布器同时
生成 30 分钟、日线、周线及身份清单，策略价格与执行价格按交易日严格对齐。

### 查看正式策略

当前正式身份为`S001 / 综合基线策略 / v1`，资格为`PAPER_READY`：

```powershell
.\.venv\Scripts\czsc-trader.exe strategy list
.\.venv\Scripts\czsc-trader.exe strategy show --strategy S001 --version v1
.\.venv\Scripts\czsc-trader.exe strategy history --strategy S001
.\.venv\Scripts\czsc-trader.exe strategy performance --strategy S001 --version v1
.\.venv\Scripts\czsc-trader.exe strategy validate --all
```

`RESEARCH`表示版本仍可修改；冻结后进入`PAPER_READY`并可创建模拟账户；人工晋升到
`LIVE_READY`后才具备实盘部署资格；`RETIRED`禁止创建新运行实例。资格变化只改变
治理权限，不直接启动、暂停或停止PTE。

### 评估候选并接受冠军

新研究在`experiments/MMDD_EXXX/`准备不可变的`evaluation_protocol.json`和
`candidate_manifest.json`。清单必须包含完整可执行策略payload、统一窗口和完整试验
台账。Trader统一运行候选并生成一页结论：

```text
experiments/MMDD_EXXX/
├─ evaluation_protocol.json   # 目标、截止日、在位策略、窗口、收紧边界
└─ candidate_manifest.json    # symbol、成本、资金、windows、candidates、trials
```

每个`candidates[]`至少提供`candidate_id`、候选/行为/执行规则哈希、`is_incumbent`和
完整`strategy_payload`；可能成为冠军的候选还需`strategy_id`与`strategy_name`。
`trials[]`覆盖全部尝试，包括早期失败和行为重复项。`target_requirements`使用窗口派生的
`<window_id>_return`、`total_return`或`net_cagr`作为首版目标字段。

```powershell
.\.venv\Scripts\czsc-trader.exe strategy evaluate --experiment 0903_EXXX
```

结论只有`RECOMMEND_FREEZE`（建议冻结）、`KEEP_INCUMBENT`（保留在位策略）和
`INSUFFICIENT_EVIDENCE`（证据不足）。决策采用净年化收益、最大回撤、卡玛比率和
盈亏因子四项核心指标，同时检查多窗口最差表现、Pareto关系、复现性、邻域和加倍成本
压力。实验只能收紧`opc-v1`默认边界，不能放宽。

评估不会自动改变策略状态。人工确认后运行：

```powershell
.\.venv\Scripts\czsc-trader.exe strategy accept-evaluation `
  --experiment 0903_EXXX --actor tomxiao --reason "确认冻结并进入模拟盘"
```

该命令幂等创建并冻结SM版本，再创建默认10万元PTE虚拟账户。PTE暂时不可用时记录
`PAPER_ACTIVATION_PENDING`；修复运行环境后重复同一命令即可继续注册。已完成实验受
输入哈希保护，历史实验档案不会被重写。

### 查看历史基线

```powershell
.\.venv\Scripts\czsc-trader.exe baseline list
.\.venv\Scripts\czsc-trader.exe baseline show `
  --version baseline_20260903 --symbol 588080.SH
.\.venv\Scripts\czsc-trader.exe baseline validate `
  --version baseline_20260903 --symbol 588080.SH
```

`baseline_20260903`是`S001-v1`的只读历史别名。旧基线继续用于历史回测和审计，
新冻结版本统一登记到`configs/strategies/`。

### 回测

```powershell
.\.venv\Scripts\czsc-trader.exe backtest run `
  --symbol 588080.SH --asset etf `
  --start 2026-01-01 --end 2026-08-21
```

回测会同时给出“活动基线·次日开盘”信号参照和“完整基线·实际执行”。后者直接
使用完整基线内嵌的费率、100份整数手、0.001元委托价档位和保守成交规则，并生成
`execution_orders*.csv`与`execution_equity*.csv`；不存在独立执行规则版本选择。

普通结果写入被 Git 忽略的 `outputs/`。正式研究证据必须归档到
`experiments/MMDD_EXXX/`，并可用以下命令校验：

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all
```

### 生成交易决策

按账户可用现金生成 `advice.v4`：

```powershell
.\.venv\Scripts\czsc-trader.exe advice run `
  --symbol 588080.SH --asset etf `
  --actual-quantity 0 --available-cash 1000000 `
  --strategy S001 --strategy-version v1 `
  --format json
```

Trader 使用完整冻结基线内嵌的唯一执行规则计算限价、手续费空间和 100 份整数倍数量。输出包含
确定性 `decision_id`、信号日期、有效交易日、目标/实际/差额数量及可提交 DAY 限价
单。`advice run`不连接券商、不提交订单、不修改账户状态。

`--baseline baseline_20260903`暂时保留为输入别名，并解析到同一个`S001-v1`；新输出
始终为`advice.v4`。兼容调用可用 `--actual-quantity`配合`--position-size`生成
`advice.v1`；PTE正式运行只接受`advice.v4`。未收到明确成交回报时，实际持仓数量保持不变。

## CZSC PTE 使用

### 前置检查

启动本机 Futu OpenD，并确认存在中国市场模拟账户。实时行情权限可以缺失，此时页面
显示“行情降级”，PTE 仍按 Trader 给出的价格和数量执行模拟委托，渠道自动调价关闭。

先用 Trader 查看当前决策。确认模拟环境和订单风险后，可执行单轮运行：

```powershell
.\.venv\Scripts\pte.exe once --repo-root D:\CodeBase\czsc_trader
```

`pte once`是完整交易周期，在有效交易时段内可能提交模拟订单。

### 前台持续运行

```powershell
.\.venv\Scripts\pte.exe serve --repo-root D:\CodeBase\czsc_trader
```

观测与干预页面为 <http://127.0.0.1:8080>。默认行为：

- 订单和成交约每 5 秒对账；账户与持仓每 60 秒刷新；
- 每个交易日 19:00 后发布完整收盘数据，失败后退避重试；
- 只有数据身份或实际持仓变化时重新生成决策；
- 自动买入尽可能使用全部已对账可用现金，卖出清空已成交持仓；
- 新单只在有效交易日的 `09:30–11:30`、`13:00–14:57`提交；
- 暂停只阻止新订单，已有订单继续对账；撤单必须二次确认。

首次启动会创建初始资金10万元、账户ID为`s001-v1`、名称为“S001-v1模拟账户”且
绑定`S001-v1`的虚拟账户。虚拟账户完全由本地账本
模拟成交，与唯一Futu模拟账户相互隔离；Futu渠道异常不会阻断虚拟账户。账户管理命令：

```powershell
.\.venv\Scripts\pte.exe account list --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte.exe account create --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-shadow --name "S001影子账户" `
  --strategy S001 --strategy-version v1 --futu-reference
.\.venv\Scripts\pte.exe account pause --repo-root D:\CodeBase\czsc_trader --account-id s001-shadow
.\.venv\Scripts\pte.exe account resume --repo-root D:\CodeBase\czsc_trader --account-id s001-shadow
```

虚拟订单在有效交易日的19:00数据发布成功后，先用完整日线按保守规则结算，再生成
下一有效交易日决策。等价触及限价但未穿价记为“触价未穿价”，不计成交。
Futu新订单会固化虚拟账户、策略版本和决策ID，并在“订单与虚拟账户”列表逐笔展示；
历史订单缺少原始证据时显示“历史未记录”。`--futu-reference`用于同一策略版本存在
多个虚拟账户时明确订单审计归属，不会复制订单；任一时刻最多一个虚拟账户带有该
标记。`account create`默认初始资金为10万元，需要其他金额时再显式
传入`--initial-cash`。页面顶部的“查看系统事件”可查看当前活动告警及数据发布、
调度失败和恢复历史；页面时间统一按北京时间展示。

需要登记模拟盘里程碑时，先由PTE导出自包含证据包，再由Trader写入策略注册表：

```powershell
.\.venv\Scripts\pte.exe performance export --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-shadow --recorded-by tomxiao `
  --start 2026-09-03 --end 2026-12-03 --output state\paper-forward.json
.\.venv\Scripts\czsc-trader.exe strategy evidence add `
  --input state\paper-forward.json
.\.venv\Scripts\czsc-trader.exe strategy performance --strategy S001 --version v1
```

日常净值仍保存在SQLite；只有人工复核、晋升或降级所需的里程碑证据进入Git。

### Windows watchdog 服务

在管理员 PowerShell 中执行：

```powershell
.\.venv\Scripts\pte-watchdog.exe install-config --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte-watchdog.exe start --wait 30
```

系统服务名为`CZSC-PTE-Watchdog`，启动类型为自动。watchdog 通过 CLI 启动 PTE
子进程，每 10 秒检查进程和 8080 HTTP 状态；连续 3 次失败后按 5、30、60 秒
退避重启。

```powershell
sc.exe query CZSC-PTE-Watchdog
.\.venv\Scripts\pte-watchdog.exe restart --wait 30
.\.venv\Scripts\pte-watchdog.exe stop --wait 30
.\.venv\Scripts\pte-watchdog.exe remove
```

本机运行数据库、发布数据和日志位于`state/paper_trading/`，均不纳入 Git。详细运行
语义见[独立包说明](packages/paper_trading_engine/README.md)。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest packages\strategy_manager\tests -q
.\.venv\Scripts\python.exe -m pytest packages\strategy_evaluator\tests -q
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests -q
node --test packages\paper_trading_engine\tests\js\console_state.test.mjs
.\.venv\Scripts\czsc-trader.exe archive validate --all --repo-root .
.\.venv\Scripts\python.exe -m ruff check `
  src tests packages\strategy_manager packages\strategy_evaluator `
  packages\paper_trading_engine\src packages\paper_trading_engine\tests
```

Trader默认测试由8个完整功能场景组成，不联网、不读取运行中的PTE，也不重算历史候选
全集。完整实验档案校验使用`archive validate --all`独立执行。TDD阶段产生的临时聚焦
用例，在相应行为进入FT-T01至FT-T08后删除。
