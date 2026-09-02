# CZSC Trader / CZSC PTE

本仓库包含两个并列运行包：

| 包 | 目录 | 命令 | 职责 |
| --- | --- | --- | --- |
| CZSC Trader | `src/czsc_trader/` | `czsc-trader` | 行情验证、冻结基线、回测、研究归档和交易决策 |
| CZSC PTE | `packages/paper_trading_engine/` | `pte`、`pte-watchdog` | 模拟账户对账、自动下单、审计、观测页面和进程保活 |

`packages/dataflows/`是两个包共用的行情获取与发布依赖。Trader 通过
`advice.v3` JSON 契约向 PTE 提供决策；PTE 不导入 Trader 的内部模块，券商渠道
不参与策略计算、定价或改量。

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
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
```

确认三个入口均可用：

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

### 查看与验证活动基线

```powershell
.\.venv\Scripts\czsc-trader.exe baseline list
.\.venv\Scripts\czsc-trader.exe baseline show `
  --version baseline_20260903 --symbol 588080.SH
.\.venv\Scripts\czsc-trader.exe baseline validate `
  --version baseline_20260903 --symbol 588080.SH
```

未显式指定版本时，回测和 advice 使用
`configs/rule_baselines/registry.json`中登记的活动基线。

### 回测

```powershell
.\.venv\Scripts\czsc-trader.exe backtest run `
  --symbol 588080.SH --asset etf `
  --start 2026-01-01 --end 2026-08-21
```

普通结果写入被 Git 忽略的 `outputs/`。正式研究证据必须归档到
`experiments/MMDD_EXXX/`，并可用以下命令校验：

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all
```

### 生成交易决策

按账户可用现金生成 `advice.v3`：

```powershell
.\.venv\Scripts\czsc-trader.exe advice run `
  --symbol 588080.SH --asset etf `
  --actual-quantity 0 --available-cash 1000000 `
  --baseline baseline_20260903 `
  --format json
```

Trader 使用完整冻结基线内嵌的唯一执行规则计算限价、手续费空间和 100 份整数倍数量。输出包含
确定性 `decision_id`、信号日期、有效交易日、目标/实际/差额数量及可提交 DAY 限价
单。`advice run`不连接券商、不提交订单、不修改账户状态。

兼容调用可用 `--actual-quantity`配合`--position-size`生成`advice.v1`；PTE 正式运行
只接受`advice.v3`。未收到明确成交回报时，实际持仓数量保持不变。

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

首次启动会创建初始资金100万元的`baseline-143`虚拟账户。虚拟账户完全由本地账本
模拟成交，与唯一Futu模拟账户相互隔离；Futu渠道异常不会阻断虚拟账户。账户管理命令：

```powershell
.\.venv\Scripts\pte.exe account list --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte.exe account create --repo-root D:\CodeBase\czsc_trader `
  --account-id range-2 --name "Range候选2" `
  --baseline baseline_20260903 --initial-cash 1000000
.\.venv\Scripts\pte.exe account pause --repo-root D:\CodeBase\czsc_trader --account-id range-2
.\.venv\Scripts\pte.exe account resume --repo-root D:\CodeBase\czsc_trader --account-id range-2
```

虚拟订单在有效交易日的19:00数据发布成功后，先用完整日线按保守规则结算，再生成
下一有效交易日决策。等价触及限价但未穿价记为“触价未穿价”，不计成交。

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
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests -q
.\.venv\Scripts\python.exe -m pytest tests -q -m archive
.\.venv\Scripts\python.exe -m ruff check `
  src tests packages\paper_trading_engine\src packages\paper_trading_engine\tests
```

默认测试不联网。完整实验档案校验使用显式`archive`标记，避免拖慢日常开发。
