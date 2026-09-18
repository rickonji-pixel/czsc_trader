# 技术交接

> 本文是跨机器、跨会话继续开发的入口，只记录系统全貌、关键边界、恢复方法和开发规则。
> 研究目标、当前结论和工作流见`research/README.md`，安装与日常操作见`docs/USER_GUIDE.md`，
> 历史设计与实施过程见`docs/superpowers/`。

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
均为仓库内独立包，只通过明确契约协作。Search、Feature Mining和`news_events`是研究脚本
可自由使用的能力，不属于TDR治理职责；正式策略结论必须通过TDR三道人工闸门进入生命周期。

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
- SRT/PTE机器契约：普通决策为`advice.v4`，原子时点计划为`advice.v5`，纯绘图为
  `account_observation.v1`。
- PTE控制台：<http://127.0.0.1:8080>。
- WDG Windows服务：`CZSC-PTE-Watchdog`。
- 当前唯一交易渠道：Futu中国市场模拟交易。

每次接手先执行：

```powershell
git status --short --branch
git log -5 --oneline
git rev-list --left-right --count origin/master...master
```

## 总体架构

```text
Tushare → DFLS → data/raw（研究池） / data/backtest（普通回测）
研究脚本 → 候选SRT + 候选快照 + 最终评价目标 → TDR
    TDR → SRT + DFLS → data/review（不可变审核快照）
        → SRT + TXE → 独立复算账本 → SE数值审计 → SGC裁判印章
        → 人工批准 → SM冻结版本（同一SRT实现及参数）

候选/冻结版本 → SRT Runner → TXE HistoricalExecutor → 历史执行账本
SM冻结版本   → SRT Runner → PTE Futu渠道 → Futu模拟账户
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
- **SRT**位于`packages/strategy_runtime/`。它把`StrategyCandidate`或SM冻结版本投影为可执行策略，声明并通过DFLS
  发布数据，计算决策与执行计划，并定义宿主通用的`ExecutionChannel`协议；源码闭包、
  候选或冻结身份和参数共同形成运行身份。候选没有虚构的`v1`身份，冻结前后使用同一实现。
  SRT不实现回测或券商渠道。
- **TXE**位于`packages/trading_execution_engine/`。它提供研究、回测和冻结复核共享的成交、
  滑点、费用、现金、持仓与净值计算；`HistoricalExecutor`实现SRT渠道协议并管理隔离的历史
  账本及幂等请求。它不生成信号、不获取数据、不管理策略生命周期或真实券商状态。
- **PTE**位于`packages/paper_trading_engine/`。它直接加载SRT，管理账户分账、决策、订单意图、
  Futu回报、调度、SQLite审计和控制台，不导入TDR、SM或SE。
- **WDG**位于PTE包内。它只负责PTE子进程生命周期和HTTP探活，不包含交易业务逻辑。
- **dataflows**负责Tushare数据获取、复权、多频发布和清单；TDR消费已发布数据。
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
5. 研究、回测和运行时决策必须遵守因果时间边界。市场数据与策略附加数据使用一致截止日，
   发布失败必须明确失败并阻止决策；只有全部声明输入满足各自截止规则、历史深度和身份校验
   才能返回`READY`。普通回测只读取已发布数据，不在回测过程中修改数据；请求窗口超出已
   发布交易日时必须失败，禁止静默截短窗口后返回成功。
6. 正式策略通过SRT运行；SM管理身份和资格，SE执行确定性数值审计，TXE统一研究、回测和
   冻结复核的执行口径，PTE只部署`PAPER_READY`版本。冻结前必须能解析并校验对应SRT实现、
   源码闭包和运行身份；冻结策略的回测与模拟盘均直接走SRT单一路径。研究、模拟盘和未来
   实盘证据分阶段保存，不得相互替代。冻结与PTE账户创建是两个独立授权动作。
7. WDG只负责PTE进程启动、探活和故障拉起，不包含交易、数据发布或账户状态判断。
8. 批处理只有全部目标完成才可更新成功日期或成功状态。部分标的发布成功、部分账户决策成功、
   渠道仅受理委托或外部结果未知，都不能汇总成全局成功；失败事实必须进入审计、告警和退避。
9. 新版`StrategyVersion`同时维护运行身份和治理身份：`release_hash`覆盖SRT执行所需的版本号
   与`strategy_payload`；`governance_hash`覆盖SGC凭据、候选、评价合同、裁判报告和人工批准
   印章引用。部署时SM必须验证完整SGC哈希链、最终冻结印章、版本记录、正式证据和生命周期
   事件。历史版本继续保留原release hash，并以唯一的`LEGACY_GOVERNANCE_ACCEPTED`事件证明
   已完成治理迁移。

### 三道治理闸门

1. `research create`创建新StrategyFamily及首条SGC；同一策略族启动后续研究批次时，输入中
   必须显式给出新的`credential_id`，原SGC保持不可变。
2. `strategy review open/evaluate/show`锁定候选和最终EvaluationMandate。TDR禁用缓存复用，
   按截止日完整数据、候选真实执行规则和`TXE-v1`语义独立复算，并阻断任何缺项或口径漂移。
3. `strategy freeze`在人工批准后重新校验证据文件、SRT输入与执行契约及整条SGC，再原子创建
   StrategyVersion。该命令不创建PTE账户；PTE部署需要单独授权。

送审前必须完成候选SRT，声明模块、类、源码闭包及其哈希、参数和完整输入契约；研究者宜在
搜索前完成实现，以复用同一SRT与TXE。旧`rule`字典不能绕过候选运行身份校验。

复算由TDR的`application/review_data.py`组织：根据实验`review_data_sources`读取研究池内
显式哈希绑定的标准化输入，经SRT与DFLS发布至`data/review/<凭据ID>/<送审哈希>/`。发布失败
不产生可用快照；审核过程离线运行，无隐式联网补数。评估制品写入快照目录下独立实验副本，
不覆盖原实验。重复读取已通过报告和正式冻结都重新验证封存证据，不能让缓存掩盖证据漂移。

候选与冻结回测共同通过`backtesting/srt_bridge.py`接入SRT与TXE。旧策略解析器已退出当前
候选评估和回测路径；只读`baseline`历史查看入口仍保留。历史实验按档案规则供人工审阅，
不承诺旧脚本可在新架构重放；已有五个冻结版本的身份与执行行为继续维护。

## 新机器恢复

```powershell
git clone https://github.com/tomxiao/czsc_trader.git czsc_trader
cd czsc_trader
git checkout master
git pull --ff-only origin master
```

依赖安装、凭据、PTE/WDG启停和健康检查统一按[用户使用说明](USER_GUIDE.md)执行。
开发环境恢复后运行本文的完整功能测试。仓库移动会改变WDG保存的绝对路径，需要按用户
手册重新安装服务配置。

## 本地状态与跨机边界

以下内容被Git忽略，需要在目标机器单独恢复：

- `.venv/`：解释器和依赖；
- `.env`：本机凭据；
- `data/raw/`、`data/backtest/`及`data/review/`：分别恢复受控研究输入、回测发布代次与封存审核证据；
- `experiments/**/artifacts/`：本机研究制品；公开克隆只承诺人工查阅，不保证历史重放或部署可用；
- `.tmp/`：测试缓存、测试运行目录和业务发布前的暂存工作区；
- `outputs/`：普通回测输出；
- `state/paper_trading/runtime.db`：账户、订单、成交、暂停状态和审计事件；
- `state/paper_trading/backups/`：PTE启动前生成的SQLite滚动备份；
- `state/paper_trading/data/`与`logs/`：运行数据副本和日志；
- `state/paper_trading/charts/`：按账户和内容指纹生成的可重建观察图缓存；
- Windows服务、Futu OpenD及其登录状态。

跨机继续开发可以创建新的本地状态。跨机延续同一条模拟盘观察序列，需要迁移完整SQLite，
并与Futu活动订单、成交和持仓逐笔核对；核对完成前保持新单阻塞。

## OPC测试用例治理

测试目标是用尽量少的稳定业务场景保护TDR、DFLS、FSC、STC、SM、SE、SRT、TXE、PTE和WDG的
完整能力。TDD最小失败用例可以临时存在；行为稳定后应并入长期功能场景并删除重复用例。
长期用例验证公开入口、关键状态转换、持久化结果和安全约束，不围绕私有实现持续增长。

用例准入、收敛与删除条件、分级回归命令、月度及触发式审查流程统一见
[测试用例治理](TEST_GOVERNANCE.md)。该文档是后续周期性治理的唯一操作规范。

治理流程重构验收使用[scripts/README_GOVERNANCE_ACCEPTANCE.md](../scripts/README_GOVERNANCE_ACCEPTANCE.md)
中的隔离CLI演练；真实行情回放另用`scripts/acceptance_srt_txe.py`。合成数据验收软件行为，
真实行情对比检查执行迁移，两者均不替代策略研究结论或生产部署授权。

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
- 根目录`README.md`只维护项目介绍和文档索引；`docs/USER_GUIDE.md`维护安装与当前
  操作路径；本文只维护长期有效的架构、边界、恢复方法和开发约束。调试流水及已完成
  任务不进入这些文档。

## 详细资料入口

- 项目介绍与文档索引：`README.md`
- 安装、日常使用和运行排障：`docs/USER_GUIDE.md`
- PTE对象关系与包级契约：`packages/paper_trading_engine/README.md`
- SM与SE契约：`docs/superpowers/specs/2026-09-03-strategy-manager-design.md`、
  `packages/strategy_evaluator/README.md`
- 研究批次目标、当前结论与工作流：`research/README.md`
- 已批准设计与实施计划：`docs/superpowers/specs/`、`docs/superpowers/plans/`

当前限制：PTE只实现Futu模拟交易渠道；控制台只监听localhost；同一观察序列的SQLite
跨机迁移仍需人工完成渠道核对。未复权正式账本尚未建模ETF现金分红、份额拆分等公司行动；
此类非交易变动发生时必须保持渠道差异告警并人工归属，禁止按交易费用自动调账。长期经济
绩效使用后复权研究口径，待公司行动契约完成后再与PTE账本做完整对齐。
