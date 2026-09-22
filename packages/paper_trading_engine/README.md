# 模拟交易引擎（Paper Trading Engine，PTE）

PTE是仓库内独立的模拟交易包，与SRT通过进程内接口和版本化契约协作。本文只描述包级边界、
对象模型、技术契约和开发验证。环境安装、账户操作、版本发布以及PTE/WDG启停见
[开发运维交接](../../docs/DEVELOPMENT_HANDOFF.md)。

## 职责与边界

PTE负责：

- 虚拟账户、资金分账、订单意图、成交回报和绩效快照；
- 观察由外部任务准备完成的SRT实例数据、逐账户决策调度和有效交易时段提交；
- Futu模拟交易执行与对账；
- SQLite追加式审计、控制台和账户前瞻观察图；
- 向SM导出人工确认所需的模拟盘里程碑证据。

PTE不负责准备行情或策略支持数据，也不负责策略计算、价格调整、目标仓位或委托数量计算，
这些事实由SRT提供。PTE只为每个运行环境提供隔离的数据根目录，并消费已经准备完成的实例。
PTE不导入TDR、SM或SE，也不创建或探测Futu行情连接。内部OHLC模拟成交渠道已经
移除；只有Futu累计成交回报能够改变账户现金和持仓。

WDG位于本包内，只管理PTE子进程生命周期和HTTP健康探测。数据发布时间、OpenD连接、
调度周期和交易规则均由PTE持有，WDG不得向PTE注入业务参数。

## 对象关系

```text
StrategyRelease 1 ─── N VirtualAccount
                         │
                         ├── 1 Instrument
                         ├── N Decision
                         ├── N OrderIntent ─── N FutuFill
                         └── N AccountSnapshot

FutuSimulateCnChannel 1 ─── N StrategyVirtualAccount
FutuSimulateCnChannel 1 ─── 1 ChannelReconciliationAccount
FutuSimulateCnChannel 1 ─── 1 Futu SIMULATE/CN account
```

- 一个策略虚拟账户绑定一个不可变策略发布、一个交易标的和一个渠道；
- `futu_simulate_cn`是唯一已实现的Futu渠道身份，固定对应`TrdEnv.SIMULATE`与
  `TrdMarket.CN`；未明确定义和实现的渠道、盘类型、市场组合在启动和运行时拒绝；
- 该渠道可承载多个策略虚拟账户及多个中国市场标的，渠道本身不绑定策略；
- 渠道平账账户是零初始资金的系统账户，不参与策略决策、账户比较或策略绩效；它仅记录
  经验证的Futu实际费用与策略模型费用之间的渠道差额，余额可为正或负；
- 每条决策、订单和成交必须追溯到虚拟账户、策略发布和关联ID；
- 未知活动订单或底层持仓与账户汇总不一致时，渠道阻止新单；
- 委托受理不代表成交，没有明确成交增量时保持账户账本不变。

## 外部契约

### SRT

- `advice.v4`：PTE传入实际持仓、可用资金和账户修订号，SRT返回确定性决策、目标
  数量、执行限价和订单列表；
- `advice.v5`：在v4账户事实基础上返回原子执行计划。每个计划环节包含交易时点、订单及
  可选成交依赖；PTE持久化全部环节后才接受该决策，并可在重启后恢复。当前日内轮换只在
  开盘买入`FILLED_ALL`后放行11:30卖出；买单未完整成交或错过时点时撤单、阻断后续环节
  并保留执行缺口。订单价格由SRT冻结执行策略给出，Futu自动调价始终关闭；
- `strategy_chart.v1`：PTE独立读取复权日线，并通过stdin传入策略身份、有限行情、策略输出
  和账户执行事实；SRT内的冻结策略图表实现返回HTML。图表行情不依赖策略数据准备或
  `StrategyInstance`；PTE使用外置Plotly运行库并长期缓存，图表事实变化时才加载新HTML；
- PTE只接受冻结且资格为`PAPER_READY`的策略发布。
- PTE在每个交易日20:30后按账户创建隔离的`StrategyInstance`并显式调用
  `prepare_data()`；实例自行推导并准备策略依赖。准备成功后原子更新
  `accounts/<account_id>/current.json`，随后立即计算该账户决策。
- 各账户使用独立准备、决策和退避状态；单个账户失败或数据调用阻塞不影响其他账户以及订单
  对账线程。决策失败时保留已经认证的准备结果，只重试该账户决策。
- PTE只保存账户级实例位置和`data_identity`，不解析策略输入数据集或实例私有清单。非交易日
  明确跳过；策略数据未达到当日截止日时准备失败，禁止使用旧数据生成新决策。
- 新账户创建前加载SRT并检查策略身份、标的、准备结果和渠道能力；任一检查失败即拒绝
  创建，不能留下“账户存在但无法运行”的半部署状态。
- 当前只部署SRT的`STATELESS`策略；依赖持久状态的运行时明确拒绝上线。

### Futu

- 固定使用渠道身份`futu_simulate_cn`、`TrdEnv.SIMULATE`与`TrdMarket.CN`模拟账户；
- PTE独占底层账户，所有虚拟账户订单共享该渠道容量；
- 订单状态和`dealt_qty`用于增量对账，明确回报前不改变账本；
- 策略虚拟账户持续按策略模型计算交易费用；仅在Futu实际费用已验证且处于允许范围时，
  与模型费用的差额才记入渠道平账账户，避免批次订单的费用归属改变策略比较结果；
- `TIMEOUT`按结果未知处理并持续查询当前及历史订单；未知状态、订单缺失或字段不一致立即
  阻塞，不能推断为失败或成功；
- Futu不参与策略计算、定价和改量。

### SM

PTE日常运行状态不进入SM。只有人工复核后的里程碑通过自包含证据包写入SM；策略资格、
虚拟账户状态和PTE进程状态彼此独立。

## 运行结构

- `cli.py`：`once`、`serve`、账户、绩效导出和控制接口；
- `scheduler.py`：20:30账户级准备/决策、独立退避及订单/账户对账节奏；
- `account_data_preparer.py`：把单账户准备请求委托给SRT适配器并校验结果；
- `srt_advice_client.py`：管理账户隔离实例，核对数据身份并把`ExecutionPlan`转换为PTE持久决策；
- `coordinator.py`与`account_engine.py`：账户中心编排和账本；
- `futu_gateway.py`与`futu_execution.py`：Futu交易适配与执行；
- `store.py`与`audit.py`：SQLite状态及四类追加式审计事件；
- `web.py`：localhost控制台和HTTP控制接口；
- `account_chart.py`：账户前瞻观察图缓存；
- `watchdog.py`与`windows_service.py`：无业务逻辑的进程保活层。

开发模式默认把运行数据库、行情副本、图表缓存和日志写入`state/paper_trading/`。生产模式
统一使用发布根目录下的`shared/`，版本目录只包含不可变代码和策略快照。两类目录均不进入
Git。跨机延续同一模拟盘序列需要迁移完整`shared/`，并重新核对Futu活动订单、成交和持仓。

### 单账户决策接口

控制台通过以下本地 Web 接口驱动一个虚拟账户立即决策：

```http
POST /api/virtual-accounts/{account_id}/decision
Content-Type: application/json

{}
```

该接口无需控制令牌。它先完成账户和订单安全检查，再以追加记录的方式保存新事实，并返回可区分的
业务状态：

- `DECISION_COMPLETED`：首次生成该信号日决策；
- `DECISION_REUSED`：输入身份与现有活动决策一致，复用既有记录；
- `DECISION_SUPERSEDED`：追加新决策并使旧决策失效；
- `DECISION_AND_INTENTS_SUPERSEDED`：同时使尚未提交渠道的旧订单意图失效并释放预留资金。

只有 `PENDING_SUBMIT` 或 `WAITING_DEPENDENCY` 且没有 `channel_order_id` 的意图可以随决策失效。
已有渠道订单或结果未知时返回冲突并阻止覆盖。批量调度继续由 `serve` 的调度器负责；人工驱动单个
账户应使用此接口。

控制台中的订单意图、订单和成交表均以交易时间倒序展示；同一时间使用稳定记录ID排序，
便于优先检查最近的执行事实。虚拟账户订单表的“下单时间”固定为第三列。

Futu渠道页只把Futu总资产、可用现金、已分配额度和未分配额度作为核心指标；现金差异、
估值差异和渠道平账余额位于资金对账明细。Futu总资产使用Futu当前估值，PTE账务总资产使用
最近日终价格，二者的估值时点差异不混入现金差异。页面定时更新时保留对账明细展开状态。

`serve`和`once`会竞争同一个`runtime.db`独占锁，确保同一运行状态只有一个自动交易进程。服务
启动前自动生成一致性SQLite备份并滚动保留3份。订单/账户快速对账与发布观察、决策分别
运行，外部数据任务变慢时仍保持订单对账和调度心跳。所有未完整执行的终态都保留为
“前瞻执行缺口”，人工确认前账户持续阻塞。新数据到达但旧订单尚未终结时，新决策会暂缓
到旧订单完成对账后再生成，避免重叠意图或遗漏执行。

调度器只有在全部活跃标的及策略均生成同一目标截止日的准备结果后才登记准备成功，只有全部到期账户
完成决策后才登记全局决策成功。部分标的或部分账户成功、决策逾期、任务异常和渠道结果未知
都会持久化为失败或降级状态，并进入审计、告警与退避；后续轮询不得把这些事实折叠成成功。
OpenD暂时不可用可以启动为明确的`UNAVAILABLE`降级状态，内部数据库、账本或对账异常则必须
中止相应流程并告警。

需要额外历史时点信息的策略，由其 `StrategyImplementation` 声明输入并推导范围，由
`StrategyInstance.prepare_data()`统一准备。任一输入缺失、截止日不足或哈希不符时PTE都不生成
该账户决策。PTE不识别S003、S007等策略专属数据结构。

生产构建和发布分别通过`../../scripts/pte-build.ps1`与`../../scripts/pte-publish.ps1`执行。
构建产物写入 `../../.build/pte/`，pip、uv-build 和临时目录分别位于
`../../.tmp/pte-release/pip/`、`../../.tmp/pte-release/uv-build/` 和
`../../.tmp/pte-release/temp/`，避免依赖用户级缓存权限。
`pte-release`是发布脚本使用的内部入口，不作为日常人工发布命令。

## 包级开发

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
.\.venv\Scripts\python.exe -m pytest -c pyproject.toml packages\paper_trading_engine\tests -q
node --test-isolation=none --test packages\paper_trading_engine\tests\functional\console_state.test.mjs
.\.venv\Scripts\python.exe -m ruff check `
  packages\paper_trading_engine\src packages\paper_trading_engine\tests
```

长期测试保持少量完整功能场景。开发中的聚焦TDD用例在行为并入功能场景后删除，避免按
内部实现细节扩张测试数量。PTE与SRT的集成测试通过账户级准备和决策公开契约验证当前
`current.json`格式；已退出运行路径的旧发布清单不构成兼容目标。
