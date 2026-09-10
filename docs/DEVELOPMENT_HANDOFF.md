# 技术交接

> 本文是跨机器、跨会话继续开发的入口，只记录系统全貌、关键边界、恢复方法和开发规则。
> 研究结论见`docs/RESEARCH_HANDOFF.md`，安装与日常操作见`docs/USER_GUIDE.md`，
> 历史设计与实施过程见`docs/superpowers/`。

## 模块简称

| 简称 | 全称 | 核心职责 |
| --- | --- | --- |
| TDR | CZSC Trader | 数据、策略解析、回测、研究编排和交易决策 |
| SM | Strategy Manager | 策略身份、版本、资格、证据和治理审计 |
| SE | Strategy Evaluator | 候选筛选、排名、体检和统计稳健性审计 |
| PTE | Paper Trading Engine | 虚拟账户、模拟交易执行、运行审计和观测 |
| WDG | PTE Watchdog | PTE进程开机自启、探活和故障拉起 |

后续开发、文档和讨论统一使用以上简称。SM、SE和PTE均为仓库内独立包，只通过明确
契约协作；用户操作入口集中在TDR和PTE。

## 当前交付状态

- 主开发分支：`master`；开始工作前现场确认分支和远端同步状态。
- Python：3.12。
- 正式策略：`S001-v1`、`S001-v2`和`S002-v1`均为`PAPER_READY`；S002使用
  `czsc_event_hold`事件持有型运行时。
- PTE虚拟账户：每个账户持有独立策略发布、标的和资产类型；现有三个账户各10万元，
  S001账户交易588080.SH，S002账户交易510500.SH。
- SM冻结必须携带SE冻结前体检批准书；批准书与策略、候选ID及候选哈希不一致时拒绝冻结。
- TDR/PTE机器契约：决策为`advice.v4`，纯绘图为`account_observation.v1`。
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
Tushare → dataflows → data/raw（研究池） / data/backtest（普通回测）
                                      ↓
                        TDR
              ┌──────────┼──────────┐
              ↓          ↓          ↓
             SM         SE      advice.v4
                                     ↓ CLI
                                    PTE ← WDG
                                     ↓
              N个虚拟账户 / N个标的 → Futu模拟渠道
                                     ↓
                              一个Futu模拟账户
```

### 模块边界

- **TDR**位于`src/czsc_trader/`。它拥有正式数据口径、策略执行语义、因果回测、实验编排
  和`advice.v4`决策生成，并作为SM、SE的应用入口。
- **SM**位于`packages/strategy_manager/`。它管理稳定策略ID、不可变版本、资格流转、
  绩效证据和追加式治理审计；它不管理策略进程与账户运行状态。
- **SE**位于`packages/strategy_evaluator/`。它接收TDR提供的事实，执行筛劣、Pareto排名、
  临时冠军体检及统计稳健性审计，输出判定和证据；它不读取仓库或改变SM、PTE状态。
- **PTE**位于`packages/paper_trading_engine/`。它通过CLI调用TDR，管理账户分账、决策、
  订单意图、Futu回报、调度、SQLite审计和控制台，不导入TDR、SM或SE。
- **WDG**位于PTE包内。它只负责PTE子进程生命周期和HTTP探活，不包含交易业务逻辑。
- **dataflows**负责Tushare数据获取、复权、多频发布和清单；TDR消费已发布数据。

依赖方向保持为：`TDR → SM/SE`、`PTE → TDR CLI`、`WDG → PTE进程`。

## 关键业务不变量

1. TDR拥有策略、价格、费率、目标仓位、分单和委托数量的计算权；PTE严格消费
   `advice.v4`，Futu自动调价关闭。
2. 虚拟账户是PTE业务归属中心。每个账户绑定一个不可变策略发布、一个交易标的和一个
   渠道；Futu渠道可以承载多个账户和多个中国市场标的，渠道本身不绑定策略。
3. 每笔决策、订单和成交都可追溯至虚拟账户、策略发布及关联ID。无法归属的活动订单或
   汇总持仓不一致会阻止新单。
4. 委托受理不代表成交。只有Futu明确返回的累计成交增量能够改变账户现金与持仓；没有
   明确成交回报时维持原账本。
5. PTE独占底层Futu模拟账户。内部OHLC模拟成交渠道已移除。
6. PTE只连接Futu交易接口，不创建或探测Futu行情连接。策略数据与执行限价来自TDR链路。
7. 每个交易日20:30后按运行账户涉及的全部标的发布完整收盘数据，任一标的失败则整体
   退避重试；成功后每个运行账户只针对自己的数据版本生成一次决策，重启能够补做遗漏。
8. 新单只在`valid_session`当天`09:30–11:30`或`13:00–14:57`提交。暂停只阻止
   新订单，活动订单继续对账；撤单需要二次确认。
9. WDG只管理PTE进程生命周期，并通过`/api/system/status`确认进程为`RUNNING`。
   数据发布时间、OpenD连接、交易标的与交易规则均由PTE持有；WDG不得用业务状态判定
   进程健康，也不得在启动PTE时注入业务参数。
10. SM资格、PTE账户状态和进程状态彼此独立。研究、模拟盘和未来实盘绩效分阶段记录。
11. PTE运行结果默认属于观察证据，只有经人工确认登记的里程碑快照进入SM治理链路。
12. PTE拥有前瞻行情及观察图缓存。TDR的`chart observation`只消费PTE通过stdin传入的
    有限数据，在内存绘制并由stdout返回HTML，不主动读取或保存行情。
13. TDR Backtest v2每次只接受一个不可变策略快照、一个实际回测标的、明确起止日和初始
    现金。回测可以将策略临时应用到同资产类型的其他标的，不修改策略快照或SM部署范围；
    报告与清单必须同时记录策略参考标的和实际回测标的。信号使用后复权数据，执行和估值
    使用不复权数据；结果发布前必须通过SE独立回放审计。
14. `data/raw/`是受控研究池，`data/backtest/`独立维护。普通回测不会更新数据，也不
    判定数据污染；研究数据边界由受控研究工作流负责。

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
- `outputs/`：普通回测输出；
- `state/paper_trading/runtime.db`：账户、订单、成交、暂停状态和审计事件；
- `state/paper_trading/data/`与`logs/`：运行数据副本和日志；
- `state/paper_trading/charts/`：按账户和内容指纹生成的可重建观察图缓存；
- Windows服务、Futu OpenD及其登录状态。

跨机继续开发可以创建新的本地状态。跨机延续同一条模拟盘观察序列，需要迁移完整SQLite，
并与Futu活动订单、成交和持仓逐笔核对；核对完成前保持新单阻塞。

## OPC测试用例治理

测试目标是用尽量少的稳定业务场景保护TDR、SM、SE、PTE的完整能力。TDD最小失败用例
可以临时存在；行为稳定后应并入长期功能场景并删除重复用例。长期用例验证公开入口、关键
状态转换、持久化结果和安全约束，不围绕私有实现持续增长。

用例准入、收敛与删除条件、分级回归命令、月度及触发式审查流程统一见
[测试用例治理](TEST_GOVERNANCE.md)。该文档是后续周期性治理的唯一操作规范。

## 开发与交付规则

- 普通改动使用`master`；重量级开发和研究任务先确认是否新建`codex/`分支。
- 不使用本地Git worktree；保留用户的无关修改。
- 分支内可以自主提交；合并`master`和推送远端前取得用户确认。
- 修改研究口径时同步`docs/RESEARCH_HANDOFF.md`和实验档案。
- 修改运行边界、契约或安装方式时同步本文及对应包`README.md`。
- 根目录`README.md`只维护项目介绍和文档索引；`docs/USER_GUIDE.md`维护安装与当前
  操作路径；本文只维护长期有效的架构、边界、恢复方法和开发约束。调试流水及已完成
  任务不进入这些文档。

## TDR Backtest v2维护边界

- 正式策略从`strategies/`解析；S001旧版本所需历史字节仅保存在
  `strategies/dependencies/legacy_rule_baselines/`。
- 研究候选通过`resolve_candidate_snapshot`进入同一个回放核心，生命周期资格不会阻止
  研究回测；只有PTE部署检查`PAPER_READY`。
- `backtest run --symbol`指定实际回测标的。若其与策略参考标的不同，Backtest v2只在本次
  内存回放中绑定新标的，并校验资产类型一致；策略ID、版本、内容哈希和注册内容保持不变。
- SE拥有回放证据审计，TDR只负责生成事实与适配证据。PTE的券商成交继续以渠道回报为准。
- `data update-backtest`严格保护既有30分钟线、日线和已结束周线；只有新增交易日仍位于
  当前末周时，才允许末根未完成周线滚动重算。校验或网络失败不得改变已发布数据集。
- `configs/`、命名回测窗口和外置执行规则版本已退出当前生产契约。多窗口由SE或实验脚本
  使用明确日期分别编排。

## 详细资料入口

- 项目介绍与文档索引：`README.md`
- 安装、日常使用和运行排障：`docs/USER_GUIDE.md`
- PTE对象关系与包级契约：`packages/paper_trading_engine/README.md`
- SM与SE契约：`docs/superpowers/specs/2026-09-03-strategy-manager-design.md`、
  `packages/strategy_evaluator/README.md`
- 当前研究结论与实验组织：`docs/RESEARCH_HANDOFF.md`
- 已批准设计与实施计划：`docs/superpowers/specs/`、`docs/superpowers/plans/`

当前限制：PTE只实现Futu模拟交易渠道；控制台只监听localhost；同一观察序列的SQLite
跨机迁移仍需人工完成渠道核对。
