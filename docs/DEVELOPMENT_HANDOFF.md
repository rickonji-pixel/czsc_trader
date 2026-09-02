# 开发交接

> 本文用于在新机器或新会话中恢复开发上下文。Git只携带源码、配置、测试、正式
> 行情和研究档案；虚拟环境、密钥、Futu OpenD、Windows服务、SQLite和日志均属于
> 机器本地状态。

## 当前交付状态

- 当前交付分支：`codex/pte-virtual-accounts`（合并后以`master`为准）
- Python版本：3.12
- 活动完整基线：`baseline_20260903`（候选143，内嵌唯一执行规则）
- 历史执行规则：`execution_policy_20260902`仅用于旧实验复现，不进入正式运行选择
- Trader/PTE契约：`advice.v3`
- PTE HTTP端口：`127.0.0.1:8080`
- Windows服务：`CZSC-PTE-Watchdog`
- 当前模拟渠道：Futu中国市场模拟交易

提交号和远端同步状态随开发变化，新会话必须现场确认：

```powershell
git status --short --branch
git log -5 --oneline
git rev-list --left-right --count origin/master...master
```

## 架构与调用链

```text
Tushare
  ↓ packages/dataflows
data/raw（三频后复权策略行情 + 未复权执行价格 + manifests）
  ↓
CZSC Trader
  ├─ baseline / backtest / archive
  └─ advice run → advice.v3 JSON（完整基线 + entry-cycle资金目标）
                      ↓ CLI 子进程
CZSC PTE ── SQLite审计 ── 本机HTTP控制台
  ├─ 独立虚拟账户账本
  └─ 渠道适配器 → Futu OpenD模拟账户
```

### CZSC Trader

根包位于`src/czsc_trader/`，负责数据校验、冻结策略解析、因果回测、实验档案校验和
交易决策。正式入口为`czsc-trader`，只公开`data`、`baseline`、`backtest`、
`advice`、`archive`五类资源。

### CZSC PTE

独立包位于`packages/paper_trading_engine/`，通过CLI调用Trader并消费`advice.v3`。
它负责渠道账户对账、决策缓存、订单意图、提交与成交增量、SQLite审计、观测页面和
运行调度。PTE不导入`czsc_trader.*`，因此策略包可以独立演化，只要保持CLI契约。

### Watchdog

`pte-watchdog`是唯一注册到Windows SCM的服务。它只负责启动`pte serve`子进程、
每10秒检查进程和`/api/status`，以及在连续3次失败后按5、30、60秒退避重启。
watchdog不包含交易业务代码。

## 核心契约与不变量

1. `advice.v3`输入实际成交数量、账户可用现金和可选entry-cycle目标；Trader拥有价格、费率、分单与数量计算。
2. PTE只接受`advice.v3`；正式运行只选择一个完整基线，不组合独立执行规则版本。
3. 渠道自动调价关闭，Futu不得改写Trader给出的限价或数量。
4. 委托受理不等于成交。只有渠道明确返回的累计成交增量才能更新持仓和审计记录。
5. 未收到明确成交回报时按未成交处理，不修改持仓修订号。
6. 新单只在`valid_session`当天的`09:30–11:30`或`13:00–14:57`
   （Asia/Shanghai）提交；其他时间继续观测和对账。
7. 暂停只阻止新订单；活动订单继续对账。撤单需要两分钟有效令牌和二次确认。
8. 每个交易日19:00后发布完整收盘数据，失败后退避重试；盘中价格不改变日频信号。
9. 普通文本身份先归一化LF；配置JSON使用语义哈希；原始行情CSV和二进制按字节哈希。
10. PTE运行结果不自动构成策略样本外或实盘有效性证据。
11. Futu渠道与每个虚拟账户独立失败；相同错误按5、15、30、60、300秒退避并聚合记录。
12. 虚拟订单等价触价但未穿价不计成交；只有明确模型成交才能改变虚拟现金与持仓。
13. 正式回测直接执行完整基线内嵌规则并输出`active_baseline_execution`；请求费率必须
    与完整基线费率一致，不能从外部形成另一套执行组合。

## 代码地图

| 路径 | 责任 |
| --- | --- |
| `src/czsc_trader/application/advice_service.py` | advice.v1/v2/v3决策组装与资金定仓 |
| `src/czsc_trader/backtest_runner.py` | 信号参照回测与完整基线实际执行回测 |
| `src/czsc_trader/cli/main.py` | Trader CLI参数与输出边界 |
| `src/czsc_trader/identity.py` | 可移植身份与哈希规则 |
| `configs/rule_baselines/` | 冻结基线与活动注册表 |
| `configs/execution_policies/` | 历史执行规则档案；正式运行不解析 |
| `packages/dataflows/` | Tushare适配、复权和多频发布 |
| `packages/paper_trading_engine/src/paper_trading_engine/engine.py` | 对账、决策和订单状态机 |
| `packages/paper_trading_engine/src/paper_trading_engine/store.py` | SQLite持久化与审计事件 |
| `packages/paper_trading_engine/src/paper_trading_engine/virtual_engine.py` | 独立虚拟账户结算与决策 |
| `packages/paper_trading_engine/src/paper_trading_engine/coordinator.py` | Futu渠道与虚拟账户故障隔离 |
| `packages/paper_trading_engine/src/paper_trading_engine/futu_gateway.py` | Futu模拟渠道适配 |
| `packages/paper_trading_engine/src/paper_trading_engine/scheduler.py` | 数据、账户、决策和订单轮询 |
| `packages/paper_trading_engine/src/paper_trading_engine/dashboard.py` | 本机观测与干预页面 |
| `packages/paper_trading_engine/src/paper_trading_engine/watchdog.py` | PTE子进程和HTTP探活 |
| `packages/paper_trading_engine/src/paper_trading_engine/windows_service.py` | Windows SCM薄适配层 |
| `experiments/` | 不可变研究档案；不能用`outputs/`替代 |

## 新机器恢复

```powershell
git clone https://github.com/tomxiao/czsc_trader.git czsc_trader
cd czsc_trader
git checkout master
git pull --ff-only origin master

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
```

恢复后先检查虚拟账户，再启动服务：

```powershell
.\.venv\Scripts\pte.exe account list --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte.exe account create --repo-root D:\CodeBase\czsc_trader `
  --account-id baseline-143 --name 候选143 `
  --baseline baseline_20260903 --initial-cash 1000000
.\.venv\Scripts\pte.exe serve --repo-root D:\CodeBase\czsc_trader
```

账户创建具有不可变身份：相同ID、名称、标的、完整基线SHA和初始资金可幂等复用，任一项
不同都会停止。SQLite位于`state/paper_trading/runtime.db`且不随Git跨机同步。

创建或切换页面中的Futu参照账户时，在`account create`末尾增加`--futu-reference`；
该标记不改变Futu渠道的决策或订单。

发布行情时将根目录`.env.example`复制为`.env`并填写`TUSHARE_TOKEN`；不要提交
密钥。运行PTE前安装并启动Futu OpenD，确认模拟账户可访问。行情权限缺失时PTE允许以
`DEGRADED_QUOTE`运行，委托仍严格使用Trader输出。

## 最小开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests -q
.\.venv\Scripts\python.exe -m ruff check `
  src tests packages\paper_trading_engine\src packages\paper_trading_engine\tests
```

完整验证：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests -q
.\.venv\Scripts\python.exe -m pytest tests -q -m archive
```

默认测试不联网。`archive`测试会校验全部不可变实验档案，耗时高于日常测试。

## PTE启动与服务恢复

先确认当前决策，不连接券商：

```powershell
.\.venv\Scripts\czsc-trader.exe advice run `
  --symbol 588080.SH --asset etf `
  --actual-quantity 0 --available-cash 1000000 `
  --format json
```

前台运行：

```powershell
.\.venv\Scripts\pte.exe serve --repo-root D:\CodeBase\czsc_trader
```

Windows服务需要在每台机器上用管理员PowerShell重新注册：

```powershell
.\.venv\Scripts\pte-watchdog.exe install-config --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte-watchdog.exe start --wait 30
sc.exe query CZSC-PTE-Watchdog
```

服务安装会写入绝对仓库路径，因此仓库移动后需要重新运行`install-config`。页面地址为
<http://127.0.0.1:8080>。

## 本机状态与跨机边界

以下内容被Git忽略，默认不会跨机同步：

- `.venv/`：Python解释器和依赖；
- `.env`：Tushare等本机凭据；
- `outputs/`：普通回测输出；
- `state/paper_trading/runtime.db`：PTE订单、成交、暂停状态和审计事件；
- `state/paper_trading/data/`：PTE运行时发布的数据副本；
- `state/paper_trading/logs/`：watchdog和PTE日志；
- Windows服务注册、Futu OpenD安装与账户登录状态。

跨机继续开发可以重新创建这些状态。跨机继续同一条模拟交易观察序列时，需要单独迁移
SQLite并与渠道订单逐笔核对；没有核对前不得推断订单或成交已经延续。

## 常见故障检查

```powershell
# 端口占用
Get-NetTCPConnection -LocalPort 8080 -ErrorAction SilentlyContinue

# 服务和父子进程
Get-CimInstance Win32_Service -Filter "Name='CZSC-PTE-Watchdog'"
Get-CimInstance Win32_Process | Where-Object Name -eq 'pte.exe'

# HTTP探活
Invoke-RestMethod http://127.0.0.1:8080/api/status

# 日志
Get-Content state\paper_trading\logs\watchdog.log -Tail 100
Get-Content state\paper_trading\logs\pte.log -Tail 100
```

端口被占用时PTE明确失败，不终止未知进程。恢复运行失败时先确认本进程已经完成一次
渠道对账。数据发布告警应结合19:00后的日志判断，盘中未获得完整收盘数据不触发正式
信号更新。

## 开发与交付规则

- 普通改动默认在`master`开发；重量级开发先与用户确认新分支。
- 不创建本地Git worktree。
- 可以在当前分支提交；合并`master`和推送远端前必须获得用户确认。
- 保留无关的本地修改，不使用破坏性Git命令清理工作区。
- 功能或修复采用测试先行；提交前至少运行受影响测试和Ruff。
- 修改研究口径时同步更新研究设计、实验档案和`docs/RESEARCH_HANDOFF.md`。
- 修改运行边界、契约或安装方式时同步更新本文和根`README.md`。

## 已知运行限制

- 当前只实现Futu模拟渠道；PTE边界允许后续增加其他渠道适配器。
- 当前机器已验证watchdog开机启动配置、PTE父子进程、HTTP探活和服务重启；直接强杀
  LocalSystem所属PTE子进程的演练被Windows权限拒绝，进程退出和HTTP连续失败恢复
  由自动化测试覆盖。
- 实时行情权限不是当前自动提交的前提；页面会明确显示行情降级状态。
- 观测页面只监听localhost，不提供远程访问、用户认证或多账户管理。
