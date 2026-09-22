# 开发运维交接

> 本文是跨机器、跨会话继续开发的入口，只记录系统全貌、关键边界、恢复方法和开发规则。
> 研究目标、当前结论、研究命令和工作流见`research/README.md`；本文维护系统架构、开发环境、
> 测试规则和PTE运维。历史设计与实施过程见`docs/superpowers/`。

## 模块与简称

| 简称 | 全称 | 核心职责 |
| --- | --- | --- |
| TDR | CZSC Trader | 正式策略结论的可信裁判员和策略生命周期维护入口 |
| DFLS | Dataflows | Tushare数据获取、复权、多频处理和数据发布 |
| FSC | Factor & Signal Catalog | 项目级信息族、因子和信号定义目录 |
| STC | Strategy Template Catalog | 策略函数模板、输入角色和参数边界目录 |
| SM | Strategy Manager | 策略身份、版本、资格、证据和治理审计 |
| SE | Strategy Evaluator | 候选筛选、排名和统计稳健性数值计算 |
| SRT | Strategy Runtime | 候选与冻结策略共用的数据契约、决策计算、执行计划和运行身份 |
| TXE | Trading Execution Engine | 统一成交、费用、现金、持仓和净值计算口径 |
| PTE | Paper Trading Engine | 虚拟账户、模拟交易执行、运行审计和观测 |
| WDG | PTE Watchdog | PTE进程开机自启、探活和故障拉起 |

后续开发、文档和讨论统一使用以上名称。DFLS、FSC、STC、SM、SE、SRT、TXE和PTE
均为仓库内独立包，只通过明确契约协作。研究脚本可以直接使用Optuna、特征提取库及其他研究
依赖；仓库不再维护通用Search和Feature Mining运行模块。`news_events`仍是TDR内的受控抽取
能力。正式策略结论必须通过TDR三道人工闸门进入生命周期。

## 当前交付状态

- 主开发分支：`master`；开始工作前现场确认分支和远端同步状态。
- Python：3.12。
- 正式策略：`S001-v1`、`S001-v2`、`S002-v1`、`S003-v1`和`S007-v1`均为
  `PAPER_READY`。S002使用`czsc_event_hold`事件持有型运行时，S003使用成分资金流宽度
  日内轮转运行时，S007使用多源机会风险门控运行时。
- PTE虚拟账户：每个账户持有独立策略发布、标的和资产类型；现有五个账户各10万元。
  S001与S007账户交易588080.SH，S002与S003账户交易510500.SH；左侧入口按交易标的代码、
  策略编号和版本依次排序。
- 新候选冻结必须绑定不可变候选快照、最终EvaluationMandate、TDR裁判报告、人工冻结决议和
  SRT运行时验收；任一身份或哈希不一致时拒绝冻结。历史五个版本以
  `LEGACY_GOVERNANCE_ACCEPTED`事件保留当时治理事实。
- SRT/PTE机器契约：普通决策为`advice.v4`，原子时点计划为`advice.v5`，统一图表上下文为
  `strategy_chart.v1`。TDR与PTE只提供事实，回测图和前瞻观察图均由冻结在SRT的策略图表
  实现生成；未声明图表契约或渲染失败时直接报错。
- PTE控制台：<http://127.0.0.1:8080>。
- WDG Windows服务：`CZSC-PTE-Watchdog`。
- 当前唯一交易渠道：Futu中国市场模拟交易。
- 仓库最新发布版本为`v0.5.10`；截至2026-09-22只读核验，PTE生产环境运行`v0.5.10`
  （提交`c2ae818`，状态`RUNNING`）。当前SRT重构仍位于开发分支，尚未合并或发布。PTE采用
  附注tag构建和独立生产环境发布，生产版本目录
  不可变，账户、SRT准备数据、配置和日志集中在共享运行目录。

每次接手先执行：

```powershell
git status --short --branch
git log -5 --oneline
git rev-list --left-right --count origin/master...master
```

## 总体架构

```text
Tushare → DFLS（获取、校验、按供应商与标的修复）
                    ↓ DataResult
研究脚本 → 候选SRT + 候选快照 + 最终评价目标 → TDR
    TDR → SRT → DFLS → data/review（不可变审核快照）
        → SRT + TXE → 独立复算账本 → SE数值审计 → SGC裁判印章
        → 人工批准 → SM冻结版本（同一SRT实现及参数）

候选/冻结版本 → StrategyRuntime.create → StrategyInstance
                                      ├─ prepare_data → DFLS
                                      └─ run_window → TXE HistoricalExecutor → 历史执行账本

SM冻结版本 → srt-prepare → 隔离的StrategyInstance数据目录
                              ↓
                PTE → plan_at → PTE Futu渠道 → Futu模拟账户
                 ↑
            WDG进程托管
```

### 模块边界

- **TDR**位于`src/czsc_trader/`。它在人工批准研究立项、候选送审和正式冻结三个节点介入，
  负责核实研究主张、组织完整体检、签发裁判报告并维护策略生命周期。实验脚本可自由使用
  新数据与算法库；只有提交冻结流程的结论才进入TDR强约束。TDR维护正式
  工作流编排、报告和图表；历史渠道、成交、费用与账户账本统一由TXE的`HistoricalExecutor`维护，
  不再保留`BacktestChannel`包装层。
- **FSC**位于`packages/factor_signal_catalog/`，定义数据位于`catalog/`。它记录项目级信息族、
  因子和信号的稳定语义、实现入口、参数及因果可用时间；不保存标的计算值、收益证据、实验
  结论或运行状态。TDR只读引用FSC，各研究线拥有自己的物化缓存与证据。
- **STC**位于`packages/strategy_template_catalog/`，定义数据位于`strategy_templates/`。它记录
  策略函数模板、输入职责、参数边界和实现复杂度，并生成确定性实例身份；不读取行情、不搜索
  参数、不回测、不评价候选。TDR只读引用STC，负责交叉核对FSC输入，并在具体研究实现中落实
  模板语义。
- **SM**位于`packages/strategy_manager/`。它持久化`StrategyFamily`、追加式
  `StrategyGovernanceCredential`、冻结版本、资格和生命周期事件；候选快照、最终
  `EvaluationMandate`、裁判报告和人工批准均作为同一凭据链上的印章内容保存。它不计算绩效，
  不管理策略进程与账户运行状态。
- **SE**位于`packages/strategy_evaluator/`。它接收TDR提供的结构化事实，执行筛劣、Pareto
  排名、PBO、DSR、Bootstrap、参数邻域和成本压力等确定性数值计算；它不读取仓库、不理解
  金融语义，也不签发TDR裁决或改变SM、PTE状态。
- **SRT**位于`packages/strategy_runtime/`。`StrategyRuntime`校验候选或冻结版本并创建
  `StrategyInstance`；实例根据交易窗口自主推导信号日、历史范围和全部数据依赖，通过DFLS
  准备并认证数据，再计算目标仓位、参考价与渠道无关的`ExecutionPlan`。源码闭包、候选或冻结
  身份和参数共同形成运行身份。调用方只提供隔离可写的数据目录，不理解或传递策略数据集。
  SRT不实现回测、账户账本或券商渠道。
- **TXE**位于`packages/trading_execution_engine/`。它提供研究、回测和冻结复核共享的成交、
  滑点、费用、现金、持仓与净值计算；`HistoricalExecutor`实现SRT的`WindowExecutor`协议，
  由`StrategyInstance.run_window(...)`逐日驱动并管理隔离的历史账本。它不生成信号、不获取
  策略数据、不管理策略生命周期或真实券商状态。
- **PTE**位于`packages/paper_trading_engine/`。外部任务先通过`srt-prepare`为活跃冻结版本准备
  隔离实例数据；PTE观察准备结果，通过SRT公共接口恢复实例并调用`plan_at(...)`，管理账户分账、
  决策、订单意图、Futu回报、调度、SQLite审计和控制台。PTE不解析SRT私有数据清单，也不导入
  TDR、SM或SE。
- **WDG**位于PTE包内。它只负责PTE子进程生命周期和HTTP探活，不包含交易业务逻辑。
- **DFLS**负责单项数据请求的获取、规范化、统一校验、按“供应商＋标的”修复和失败阻断。
  SRT只处理`DataResult`；多输入策略的范围推导、组合认证和实例级数据身份由SRT负责。
- **新闻事件抽取**位于TDR的`news_events`独立内部包。dataflows或实验脚本负责缓存原文，
  TDR逐篇调用单一MaaS模型并执行严格结构校验、原文证据回查、断点复用和审计落盘；SE、
  SM和PTE不直接调用模型。MaaS凭据只从进程环境或Git忽略的根目录`.env`读取。

正式实验档案按`experiments/<策略ID>/<实验ID>/`保存。实验ID全局唯一，TDR按ID定位
嵌套档案；历史SM证据中的旧路径字符串保持不变，并由兼容解析器映射到当前目录。

依赖方向保持为：`TDR → FSC/STC/SM/SE/TXE/SRT/DFLS`、`TXE → SRT`、`SRT → DFLS`、`PTE → SRT`、
`WDG → PTE进程`。TXE与DFLS同层，TDR和研究脚本可以调用；PTE的账户事实仍以渠道回报为准。

## 跨模块硬约束

1. SRT拥有策略、价格、费率、目标仓位和委托参数的计算权；PTE只消费SRT决策并负责执行，
   Futu回报是订单与成交状态的事实来源，渠道不得改写策略决策。
2. 虚拟账户是PTE业务归属中心。每个账户绑定一个不可变策略发布、一个交易标的和一个渠道；
   一个渠道可以承载多个账户，渠道本身不绑定策略。PTE独占底层Futu模拟账户。
3. 决策、订单意图、渠道订单、成交和账本变动必须能够追溯到虚拟账户、策略发布及关联ID；
   无法归属的活动订单或账户汇总不一致时必须阻止新单。
4. 只有明确的累计成交增量能够改变现金和持仓。结果未知时保持原账本并持续双向对账；自动
   交易保持单写进程，意图与计划必须幂等、事务化持久，并能在重启后恢复。
5. 研究、回测和运行时决策必须遵守因果时间边界。DFLS对每个请求完成校验、必要修复和重新
   校验，无法提供准确完整数据时返回失败；`StrategyInstance.prepare_data()`负责推导并认证
   全部声明输入。普通回测只指定评价窗口和实例数据目录，不理解策略数据集；窗口或截止日
   无法满足时必须失败，禁止静默截短后返回成功。
6. 正式策略通过SRT运行；SM管理身份和资格，SE执行确定性数值审计，TXE统一研究、回测和
   冻结复核的执行口径，PTE只部署`PAPER_READY`版本。冻结前必须能解析并校验对应SRT实现、
   源码闭包和运行身份；冻结策略的回测与模拟盘均直接走SRT单一路径。研究、模拟盘和未来
   实盘证据分阶段保存，不得相互替代。冻结与PTE账户创建是两个独立授权动作。
7. WDG只负责PTE进程启动、探活和故障拉起，不包含交易、数据发布或账户状态判断。
8. 批处理只有全部目标完成才可更新成功日期或成功状态。部分策略数据准备成功、部分账户决策成功、
   渠道仅受理委托或外部结果未知，都不能汇总成全局成功；失败事实必须进入审计、告警和退避。
9. 新版`StrategyVersion`同时维护运行身份和治理身份：`release_hash`覆盖SRT执行所需的版本号
   与`strategy_payload`；`governance_hash`覆盖SGC凭据、候选、评价合同、裁判报告和人工批准
   印章引用。部署时SM必须验证完整SGC哈希链、最终冻结印章、版本记录、正式证据和生命周期
   事件。历史版本继续保留原release hash，并以唯一的`LEGACY_GOVERNANCE_ACCEPTED`事件证明
   已完成治理迁移。

### 三道治理闸门

1. `research create`创建新StrategyFamily及首条SGC；同一策略族启动后续研究批次时，输入中
   必须显式给出新的`credential_id`，原SGC保持不可变。
2. `candidate review/evaluate`锁定候选包和最终EvaluationMandate。TDR禁用缓存复用，
   按截止日完整数据、候选真实执行规则和`TXE-v1`语义独立复算，并阻断任何缺项或口径漂移。
3. `candidate freeze`在人工批准后重新校验证据文件、SRT输入与执行契约及整条SGC，再原子创建
   StrategyVersion。`strategy deploy`将冻结版本安装到SRT；PTE账户启用需要单独授权。

送审前必须完成候选SRT，声明模块、类、源码闭包及其哈希、参数和完整输入契约；研究者宜在
搜索前完成实现，以复用同一SRT与TXE。旧`rule`字典不能绕过候选运行身份校验。

复算由TDR的`application/review_data.py`组织：TDR把显式哈希绑定的执行行情封存到
`data/review/<凭据ID>/<送审哈希>/`，每个候选或冻结版本再在该快照内获得独立的SRT数据空间，
并由`StrategyInstance.prepare_data()`准备策略依赖。任何执行数据、策略输入或运行身份校验失败
都不产生可用快照；审核过程离线运行，无隐式联网补数。评估制品写入快照目录下独立实验副本，
不覆盖原实验。重复读取已通过报告和正式冻结都重新验证封存证据，不能让缓存掩盖证据漂移。

候选与冻结回测共同通过`backtesting/srt_bridge.py`接入SRT与TXE。旧策略解析器和`baseline`
命令入口均已退出当前路径，历史baseline文件只作为冻结策略的不可变身份依据保留。历史实验
按档案规则供人工审阅，不承诺旧脚本可在新架构重放；已有冻结版本的身份与执行行为继续维护。

## 新机器恢复

```powershell
git clone https://github.com/tomxiao/czsc_trader.git czsc_trader
cd czsc_trader
git checkout master
git pull --ff-only origin master
```

### 开发环境安装

项目使用Python 3.12。新建虚拟环境后按包依赖方向安装本地源码：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows
.\.venv\Scripts\python.exe -m pip install -e ".\packages\factor_signal_catalog[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_template_catalog[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_manager[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_evaluator[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_runtime[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\trading_execution_engine[test]"
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
.\.venv\Scripts\czsc-trader.exe --help
.\.venv\Scripts\pte.exe --help
```

Tushare与新闻MaaS凭据写入由Git忽略的`.env`；使用Futu模拟交易前启动Futu OpenD。开发环境
恢复后按[测试用例治理](TEST_GOVERNANCE.md)运行相应回归。WDG只绑定独立PTE生产根目录，
不引用开发仓库；迁移开发仓库无需重装WDG。

## 本地状态与跨机边界

以下内容被Git忽略，需要在目标机器单独恢复：

- `.venv/`：解释器和依赖；
- `.env`：本机凭据；
- `data/raw/`、`data/backtest/`及`data/review/`：分别恢复受控研究输入、回测执行数据与封存审核证据；
- `experiments/**/artifacts/`：本机研究制品；公开克隆只承诺人工查阅，不保证历史重放或部署可用；
- `.tmp/`：测试缓存、测试运行目录和业务发布前的暂存工作区；
- `.build/pte/`：可重建的PTE版本构建产物；
- `.tmp/pte-release/`：PTE构建使用的pip、uv-build和临时目录；
- `outputs/`：普通回测输出；
- `state/paper_trading/`：仅在显式运行开发态PTE时生成的数据库、备份、数据、图表缓存和日志；
  未运行开发态PTE时可以删除，生产PTE不会读取该目录；
- PTE生产根目录的`shared/`：生产账户、订单、成交、暂停状态、审计、SRT准备数据、配置与日志；
- Windows服务、Futu OpenD及其登录状态。

跨机继续开发可以创建新的本地状态。跨机延续同一条模拟盘观察序列，需要迁移完整SQLite，
SRT准备数据及配置组成的完整生产`shared/`，并与Futu活动订单、成交和持仓逐笔核对；核对完成
前保持新单阻塞。

## PTE发布与日常运维

PTE生产写入、服务控制和账户变更均须先取得明确授权。生产根目录以
`scripts/pte-publish.ps1`内置的`$ProductionRoot`为唯一配置来源；以下命令中的`$PteRoot`
取该值。PTE业务语义和对象关系见[PTE包级说明](../packages/paper_trading_engine/README.md)。

### 构建与发布

构建只读取指定附注tag，产物进入Git忽略的`.build/pte/`，pip、uv-build和临时目录进入
`.tmp/pte-release/`。发布校验构建
身份、安装不可变版本、切换活动版本并等待健康检查；普通版本发布不需要重新安装WDG：

```powershell
$Tag = Read-Host '请输入附注tag'
.\scripts\pte-build.ps1 -Tag $Tag
.\scripts\pte-publish.ps1 -Tag $Tag
Invoke-RestMethod http://127.0.0.1:8080/api/system/status
```

发布脚本拒绝轻量tag、提交不匹配、策略快照漂移、制品损坏和不完整的既有版本。生产目录只
保留`host/`、`releases/`和`shared/`；构建过程与缓存不写入生产目录。

### 活动版本与账户操作

生产命令必须显式绑定当前活动发布和共享状态。在同一PowerShell会话准备参数：

```powershell
$Active = Get-Content (Join-Path $PteRoot 'shared\config\active-release.json') -Raw |
  ConvertFrom-Json
$ReleaseRoot = Join-Path $PteRoot "releases\$($Active.release_id)"
$Pte = Join-Path $ReleaseRoot '.venv\Scripts\pte.exe'
$RuntimeArgs = @(
  '--repo-root'; $ReleaseRoot
  '--database'; (Join-Path $PteRoot 'shared\state\runtime.db')
  '--data-dir'; (Join-Path $PteRoot 'shared\data')
  '--config-root'; (Join-Path $PteRoot 'shared\config')
  '--advice-executable'; (Join-Path $ReleaseRoot '.venv\Scripts\czsc-trader.exe')
  '--release-manifest'; (Join-Path $ReleaseRoot 'release-manifest.json')
)
```

常用账户命令：

```powershell
& $Pte account list @RuntimeArgs
& $Pte account create @RuntimeArgs `
  --account-id s002-v1 --name "S002-v1模拟账户" `
  --strategy S002 --strategy-version v1 `
  --symbol 510500.SH --asset etf --initial-cash 100000
& $Pte account pause @RuntimeArgs --account-id s002-v1
& $Pte account resume @RuntimeArgs --account-id s002-v1
```

需要立即驱动单个虚拟账户决策时，在控制台调用：

```http
POST /api/virtual-accounts/{account_id}/decision
Content-Type: application/json

{}
```

该接口无需控制令牌，返回`DECISION_COMPLETED`、`DECISION_REUSED`、
`DECISION_SUPERSEDED`或`DECISION_AND_INTENTS_SUPERSEDED`。只有尚未提交渠道且没有
`channel_order_id`的订单意图可以随旧决策失效；已有渠道订单或结果未知时返回冲突。

冻结策略不会自动进入PTE；创建账户是独立授权动作。暂停只阻止新单，已有订单继续对账。
PTE没有策略数据准备业务入口；生产定时任务显式调用`srt-prepare`，SRT实例负责实际准备和认证，
PTE只观察准备结果、恢复实例并维护生产存储空间。准备异常必须先于账户决策明确暴露。
需要把模拟盘里程碑写回策略生命周期时，先导出自包含证据，再由TDR登记：

```powershell
& $Pte performance export @RuntimeArgs `
  --account-id s002-v1 --recorded-by tomxiao `
  --start 2026-09-03 --end 2026-12-03 --output .tmp\paper-forward.json
```

日常净值保留在PTE数据库；导出的里程碑证据须先经过人工复核。`strategy`命令仅覆盖SRT，
当前不提供通过该命令登记策略生命周期证据的入口。

### WDG、健康检查与故障处理

首次发布完成后，管理员PowerShell从已发布的轻量宿主安装WDG。普通PTE版本和策略发布继续
复用该宿主；只有WDG依赖或服务配置变化时重新执行`install-config`：

```powershell
$Watchdog = Get-ChildItem (Join-Path $PteRoot 'host\releases') `
  -Filter pte-watchdog.exe -Recurse |
  Sort-Object LastWriteTimeUtc -Descending |
  Select-Object -First 1
& $Watchdog.FullName install-config --runtime-root $PteRoot
& $Watchdog.FullName start --wait 30

Get-Service CZSC-PTE-Watchdog
Start-Service CZSC-PTE-Watchdog
Stop-Service CZSC-PTE-Watchdog
Restart-Service CZSC-PTE-Watchdog
& $Watchdog.FullName remove
```

开发调试可以运行`.\.venv\Scripts\pte.exe serve --repo-root .`，它只使用可丢弃的
`state/paper_trading/`。生产环境由WDG托管时禁止再启动第二个`serve`或并发执行`pte once`；
数据库独占锁会拒绝第二个写进程。

日常只读检查：

```powershell
Get-Service CZSC-PTE-Watchdog
Get-NetTCPConnection -LocalPort 8080 -ErrorAction SilentlyContinue
Invoke-RestMethod http://127.0.0.1:8080/api/system/status
Get-Content (Join-Path $PteRoot 'shared\logs\watchdog.log') -Tail 100
Get-Content (Join-Path $PteRoot 'shared\logs\pte.log') -Tail 100
```

生产状态、SRT准备数据、图表、配置和日志统一位于`shared/`。端口冲突、数据库写锁、准备结果
不完整、渠道订单归属不明或持仓不一致都会明确失败或阻止新单。恢复前先核对Futu当前及历史
订单、成交和持仓；禁止直接修改SQLite。只有审计证据满足受保护修复条件时才使用：

```powershell
& $Pte control repair-ledger @RuntimeArgs `
  --account-id s003-v1 --intent-id PTE-XXXXXXXXXXXXXXXXXXXX
```

成功返回`REPAIRED`，重复执行返回`ALREADY_REPAIRED`，证据不完整时明确失败。PTE每次启动
前创建SQLite备份并滚动保留3份；备份不替代Futu事实核对。

## OPC测试用例治理

测试目标是用尽量少的稳定业务场景保护TDR、DFLS、FSC、STC、SM、SE、SRT、TXE、PTE和WDG的
完整能力。TDD最小失败用例可以临时存在；行为稳定后应并入长期功能场景并删除重复用例。
长期用例验证公开入口、关键状态转换、持久化结果和安全约束，不围绕私有实现持续增长。

用例准入、收敛与删除条件、分级回归命令、月度及触发式审查流程统一见
[测试用例治理](TEST_GOVERNANCE.md)。该文档是后续周期性治理的唯一操作规范。

治理和执行链路由根目录及各独立包的长期功能场景验收：TDR覆盖三道闸门、证据漂移与失败
语义，SRT/TXE覆盖候选和冻结版本的同路径执行，PTE覆盖准备结果、账户、订单、账本与服务
配置。历史实验只保证档案校验和人工查看，不承诺旧脚本回放。完整命令及版本验收边界见
[测试用例治理](TEST_GOVERNANCE.md)；任何生产部署仍需独立授权。

最近一次仓库级治理完成于2026-09-22：42个测试文件共收集252个Python测试项和3个Node
测试项，完整离线回归及Ruff均通过。治理记录、模块耗时、保留理由和未覆盖在线检查统一维护在
[测试用例治理](TEST_GOVERNANCE.md)的“最近一次治理记录”章节；后续治理以该记录为比较基线。
完整离线回归默认运行`.\scripts\test-all.ps1`，三条模块通道全部结束后统一汇总退出码并运行
Ruff；通道日志位于`.tmp/test-regression/`。单模块失败定位命令继续以测试治理文档为准。

仓库内临时文件统一进入根目录`.tmp/`并按用途分区。业务代码通过
`czsc_trader.temp_workspace`创建临时目录；测试与Ruff分别使用`.tmp/pytest`和
`.tmp/ruff`。禁止在根目录、`data/`、`outputs/`、`experiments/`或各包目录新增临时
工作区。

## 开发与交付规则

- 普通改动使用`master`；重量级开发和研究任务先确认是否新建`codex/`分支。
- 不使用本地Git worktree；保留用户的无关修改。
- 分支内可以自主提交；合并`master`和推送远端前取得用户确认。
- 修改研究口径时同步`research/README.md`、对应`research/SXX/HANDOFF.md`和实验档案。
- 修改运行边界、契约或安装方式时同步本文及对应包`README.md`。
- 根目录`README.md`只维护项目介绍和文档索引；`research/README.md`维护研究工作流和
  研究命令；本文维护架构、开发环境、运行边界和PTE运维。调试流水及已完成任务不进入
  这些文档。

## 详细资料入口

- 项目介绍与文档索引：`README.md`
- 各子包契约：`packages/dataflows/README.md`、`packages/factor_signal_catalog/README.md`、
  `packages/strategy_template_catalog/README.md`、`packages/strategy_manager/README.md`、
  `packages/strategy_evaluator/README.md`、`packages/strategy_runtime/README.md`、
  `packages/trading_execution_engine/README.md`、`packages/paper_trading_engine/README.md`
- 研究批次目标、当前结论与工作流：`research/README.md`
- 已批准设计与实施计划：`docs/superpowers/specs/`、`docs/superpowers/plans/`

当前限制：PTE只实现Futu模拟交易渠道；控制台只监听localhost；同一观察序列的SQLite
跨机迁移仍需人工完成渠道核对。未复权正式账本尚未建模ETF现金分红、份额拆分等公司行动；
此类非交易变动发生时必须保持渠道差异告警并人工归属，禁止按交易费用自动调账。长期经济
绩效使用后复权研究口径，待公司行动契约完成后再与PTE账本做完整对齐。
