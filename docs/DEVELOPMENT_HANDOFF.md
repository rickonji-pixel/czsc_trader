# 开发交接

> 本文用于在新机器或新会话中恢复开发上下文。Git只携带源码、配置、测试、正式
> 行情和研究档案；虚拟环境、密钥、Futu OpenD、Windows服务、SQLite和日志均属于
> 机器本地状态。

## 当前交付状态

- 当前开发分支：`codex/pte-console-v2`（完成后待用户授权合并`master`）
- Python版本：3.12
- 正式策略：`S001 / 综合基线策略 / v2`，资格`PAPER_READY`
- 历史基线别名：`baseline_20260903`（候选143，内嵌唯一执行规则）
- 历史执行规则：`execution_policy_20260902`仅用于旧实验复现，不进入正式运行选择
- Trader/PTE契约：`advice.v4`
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
  ├─ strategy → Strategy Manager（身份、版本、资格、证据）
  ├─ strategy evaluate → Strategy Evaluator（筛选、排名、体检、建议）
  ├─ baseline / backtest / archive
  └─ advice run → advice.v4 JSON（策略发布身份 + entry-cycle资金目标）
                      ↓ CLI 子进程
CZSC PTE ── SQLite审计 ── 本机HTTP控制台
  ├─ 独立虚拟账户账本
  └─ 渠道适配器 → Futu OpenD模拟账户
```

### CZSC Trader

根包位于`src/czsc_trader/`，负责数据校验、冻结策略解析、因果回测、实验档案校验和
交易决策。正式入口为`czsc-trader`，公开`data`、`baseline`、`strategy`、
`backtest`、`advice`、`archive`六类资源。

### Strategy Manager

独立包位于`packages/strategy_manager/`，只依赖Python标准库，数据位于
`configs/strategies/`。它负责稳定策略ID、不可变版本、`RESEARCH -> PAPER_READY ->
LIVE_READY -> RETIRED`资格、追加式审计和分阶段绩效证据。它没有CLI、服务、页面、
SQLite或运行状态；用户统一通过Trader的`strategy`资源操作。

### Strategy Evaluator

独立包位于`packages/strategy_evaluator/`，依赖NumPy和SciPy。它拥有不可变评估契约、
版本化的`opc-v1`、`opc-v2`和`opc-v3`边界、非劣判断、最差窗口画像、Pareto分层、完整
临时冠军审计、最终判定和一页摘要。v3把既有执行、重复性、账本和压力审计，与PBO、
DSR、配对平稳区块Bootstrap及真实候选邻域检验统一内聚到SE。统计结论只产生
`FAVORABLE/MIXED/WEAK`风险标签，不设数值自动否决。它不读仓库、不执行回测、不写
文件，也不改变SM或PTE状态。生产依赖方向固定为
`czsc_trader -> strategy_evaluator`；SM和PTE均不得导入它。

Trader的`candidate_evaluation.py`统一加载行情和因子并复用正式执行模拟；
`application/evaluation_evidence.py`构建不可变收益、参数、成交、因子和压力事实；
`application/evaluation_service.py`负责实验输入、防覆盖哈希、原子产物、人工接受、SM
冻结与PTE CLI注册。每次评价同时保存快速阶段全部指标、逐项非劣比较和每个候选的筛除
去向；最终结果绑定三项筛选审计文件的内容哈希，重复读取会验证完整性。评估命令和接受
命令分别为：

```powershell
.\.venv\Scripts\czsc-trader.exe strategy evaluate --experiment 0903_EXXX
.\.venv\Scripts\czsc-trader.exe strategy accept-evaluation `
  --experiment 0903_EXXX --actor tomxiao --reason "确认冻结并进入模拟盘"
```

候选评价器支持算法与多核组合加速：共同因子和regime只计算一次，实际目标仓位相同的
候选共享交易模拟，审计与权益校验使用等价数组路径，唯一行为块通过joblib/loky并行。
`evaluation_workers`缺失时保持1；正式基准在本机选择8。历史指标复用只接受清单中
`reuse_source_experiments`明确声明且完整通过归档哈希校验的实验，不使用`state`缓存。
`artifact_reuse.csv`记录每条指标的`REUSED/COMPUTED`状态，冠军重复性检查始终现场计算。

0903_EX05实测：固定64候选相对旧参考5.14倍；1,187项候选池冷启动114.33秒；复用EX04
并补算正式与压力缺口后总运行23.08秒；结果落盘后的幂等复核5.33秒。

`0903_EX06`复用EX05完全相同的1,187项候选，按OPC-v3重新产生R1102为唯一临时冠军；
完整审计`PASS`、统计风险`MIXED`，端到端运行
125.17秒，达到5分钟目标。PBO与配对Bootstrap偏正面，DSR中性，真实候选邻域偏弱。
用户已于2026-09-03人工接受，R1102冻结为`S001-v2`并注册PTE账户`s001-v2`，初始资金
10万元。证据位于`statistical_audit.json`、收益矩阵CSV、Bootstrap/邻域/压力CSV和评估报告。

接受日志位于实验根目录`evaluation_acceptance.json`。`PAPER_ACTIVATION_PENDING`表示SM
已经冻结、仅PTE账户注册待重试；重复命令不得创建第二个版本。默认虚拟资金为10万元。

### CZSC PTE

独立包位于`packages/paper_trading_engine/`，通过CLI调用Trader并消费`advice.v4`。
它负责渠道账户对账、决策缓存、订单意图、提交与成交增量、SQLite审计、观测页面和
运行调度。PTE不导入`czsc_trader.*`或`strategy_manager`，只消费机器契约。

Console v2采用三个资源级页面：`/accounts/{account_id}`是虚拟账户主从工作区，
`/channels/futu`是唯一Futu模拟渠道，`/comparison`是只读共同区间比较。前端只消费
`/api/system/status`、账户快照、渠道快照和比较接口；兼容`/api/status`仅供watchdog。
当前账户由URL唯一确定，旧请求会被取消且迟到响应在渲染前再次校验账户身份。

Futu执行策略保存在SQLite设置`futu_strategy_binding`，通过`pte channel bind-strategy`
显式切换。命令经Trader校验资格、版本和发布哈希，并记录actor、reason及新旧绑定；
存在活动渠道订单时拒绝切换。策略冻结只创建虚拟账户，不自动改变Futu绑定。

### Watchdog

`pte-watchdog`是唯一注册到Windows SCM的服务。它只负责启动`pte serve`子进程、
每10秒检查进程和`/api/status`，以及在连续3次失败后按5、30、60秒退避重启。
watchdog不包含交易业务代码。

PTE提供`pte control restart --repo-root <repo>`作为日常无管理员权限的优雅重启入口。
CLI从运行库读取控制令牌，请求本机`POST /api/system/restart`，并等待`instance_id`变化；
PTE先将渠道和虚拟账户置为排空态、停止调度、等待当前周期结束，再以退出码0结束。
watchdog对退出码0跳过故障退避并拉起新子进程。首版不提供停止且不重拉的控制命令。

### 策略治理操作

只读检查：

```powershell
.\.venv\Scripts\czsc-trader.exe strategy list
.\.venv\Scripts\czsc-trader.exe strategy show --strategy S001 --version v2
.\.venv\Scripts\czsc-trader.exe strategy history --strategy S001
.\.venv\Scripts\czsc-trader.exe strategy performance --strategy S001 --version v2
.\.venv\Scripts\czsc-trader.exe strategy validate --all
```

创建策略和研究版本使用完整JSON输入及`--actor/--reason`；冻结还需
`RESEARCH_BACKTEST`证据，晋升还需已登记的`PAPER_FORWARD`证据。命令分别为
`strategy create`、`strategy version create`、`strategy freeze`、`strategy promote`、
`strategy downgrade`和`strategy retire`。所有失败的领域校验都不得追加资格事件。

模拟盘证据交换：

```powershell
.\.venv\Scripts\pte.exe performance export --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-shadow --recorded-by tomxiao `
  --start 2026-09-03 --end 2026-12-03 --output state\paper-forward.json
.\.venv\Scripts\czsc-trader.exe strategy evidence add --input state\paper-forward.json
```

Trader会验证证据包的来源哈希，并复制到`configs/strategies/S001/evidence/`。日常净值
继续保存在PTE SQLite，只有里程碑快照进入Git。

## 核心契约与不变量

1. `advice.v4`输入实际成交数量、账户可用现金和可选entry-cycle目标；Trader拥有价格、费率、分单与数量计算。
2. PTE只接受`advice.v4`；账户绑定`strategy_id/version/release_hash`，旧基线只作为历史别名。
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
14. SM资格与PTE运行状态相互独立；资格变化不自动启停进程或账户。
15. 研究、模拟盘和未来实盘绩效按阶段并列，不拼接为一条收益曲线。
16. 控制台URL决定资源作用域；虚拟账户、Futu渠道和账户比较使用独立页面与API。
17. Futu策略绑定必须显式、持久化且可审计；无绑定或决策身份不一致时继续对账并停止新单。
18. Futu订单提交前固化虚拟账户、策略版本、决策和意图身份；旧订单没有原始证据时
    标记“历史未记录”，不做推定关联。
19. SQLite保留带时区原始时间，控制台统一转换为北京时间展示。

## 代码地图

| 路径 | 责任 |
| --- | --- |
| `src/czsc_trader/application/advice_service.py` | advice.v1/v2/v3兼容构建与v4正式决策 |
| `src/czsc_trader/application/strategy_service.py` | Strategy Manager的Trader应用门面 |
| `src/czsc_trader/backtest_runner.py` | 信号参照回测与完整基线实际执行回测 |
| `src/czsc_trader/cli/main.py` | Trader CLI参数与输出边界 |
| `src/czsc_trader/identity.py` | 可移植身份与哈希规则 |
| `configs/rule_baselines/` | 冻结基线与活动注册表 |
| `configs/strategies/` | 正式策略、版本、生命周期和证据索引 |
| `configs/execution_policies/` | 历史执行规则档案；正式运行不解析 |
| `packages/dataflows/` | Tushare适配、复权和多频发布 |
| `packages/strategy_manager/` | 纯领域策略治理包 |
| `packages/paper_trading_engine/src/paper_trading_engine/engine.py` | 对账、决策和订单状态机 |
| `packages/paper_trading_engine/src/paper_trading_engine/store.py` | SQLite持久化与审计事件 |
| `packages/paper_trading_engine/src/paper_trading_engine/virtual_engine.py` | 独立虚拟账户结算与决策 |
| `packages/paper_trading_engine/src/paper_trading_engine/performance_export.py` | 自包含模拟盘里程碑证据导出 |
| `packages/paper_trading_engine/src/paper_trading_engine/coordinator.py` | Futu渠道与虚拟账户故障隔离 |
| `packages/paper_trading_engine/src/paper_trading_engine/futu_gateway.py` | Futu模拟渠道适配 |
| `packages/paper_trading_engine/src/paper_trading_engine/scheduler.py` | 数据、账户、决策和订单轮询 |
| `packages/paper_trading_engine/src/paper_trading_engine/web_api.py` | 系统、账户、渠道和比较资源快照 |
| `packages/paper_trading_engine/src/paper_trading_engine/channel_binding.py` | Futu策略显式绑定与迁移审计 |
| `packages/paper_trading_engine/src/paper_trading_engine/static/` | 无构建链的Console v2页面、路由和样式 |
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
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_manager[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_evaluator[test]"
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
```

恢复后先检查虚拟账户，再启动服务：

```powershell
.\.venv\Scripts\pte.exe account list --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte.exe account create --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-v2 --name S001-v2模拟账户 `
  --strategy S001 --strategy-version v2
.\.venv\Scripts\pte.exe serve --repo-root D:\CodeBase\czsc_trader
```

账户创建具有不可变身份：相同ID、名称、标的、策略版本发布哈希和初始资金可幂等复用，任一项
不同都会停止。SQLite位于`state/paper_trading/runtime.db`且不随Git跨机同步。
虚拟账户默认初始资金为10万元。启动时将旧账户`baseline-143`连同账本和账户事件
原子迁移为`s001-v1`，并统一名称为“S001-v1模拟账户”；`s001-v2`统一名称为
“S001-v2模拟账户”。资金、持仓、订单、成交和快照均保持不变，迁移另记审计事件。

页面顶部的“查看系统事件”展示当前活动告警，以及数据发布、调度失败和恢复历史；
“发布数据失败”等事件可在这里查看时间和格式化详情。

创建或切换Futu参照账户时，在`account create`末尾增加`--futu-reference`。该标记
用于同一策略版本存在多个虚拟账户时明确新订单审计归属，不改变Futu渠道的决策内容。
Futu页面通过“订单与虚拟账户”列表展示逐笔归属，不单独展示当前绑定和最新渠道决策。

发布行情时将根目录`.env.example`复制为`.env`并填写`TUSHARE_TOKEN`；不要提交
密钥。运行PTE前安装并启动Futu OpenD，确认模拟账户可访问。行情权限缺失时PTE允许以
`DEGRADED_QUOTE`运行，委托仍严格使用Trader输出。

## 最小开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q
.\.venv\Scripts\python.exe -m pytest packages\strategy_manager\tests -q
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests -q
.\.venv\Scripts\python.exe -m ruff check `
  src tests packages\strategy_manager packages\paper_trading_engine\src packages\paper_trading_engine\tests
```

完整验证：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest packages\strategy_manager\tests -q
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
  --strategy S001 --strategy-version v2 `
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
