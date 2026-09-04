# PTE账户中心化执行架构设计

日期：2026-09-04  
开发分支：`codex/pte-audit-events`  
状态：用户已确认对象模型、数据模型、运行时序及Futu独占约束

## 1. 背景与目标

现有PTE同时存在“Futu渠道绑定策略”和“虚拟账户绑定策略”两套关系，并以独立的
`PaperTradingEngine`和`VirtualAccountEngine`分别生成决策。前者依据Futu总账户资金和持仓运行，
后者依据内部账户状态运行并使用OHLC模型成交。这种双引擎结构使决策、订单、成交和审计归属
出现歧义。

本次改造将PTE统一为账户中心化模型：虚拟账户是Futu模拟账户之上的逻辑子账户；每个账户绑定
一份不可变冻结策略和一个交易渠道；当前唯一渠道为Futu；多个账户共享同一个Futu模拟账户；
Futu订单与成交是唯一交易事实。

目标包括：

- 所有决策、订单、成交和绩效都能追溯到唯一虚拟账户；
- Futu渠道支持多个虚拟账户，每个账户只绑定一个渠道；
- 每个账户使用自身分配资金生成原始订单数量，不按Futu总资金放大；
- 移除内部OHLC成交模型和渠道直接绑定策略的关系；
- 多账户在共享Futu总账户上保持资金、持仓和审计分账；
- 保持单机SQLite、原生Web控制台和OPC团队可维护性。

不增加实盘交易、多券商路由、跨渠道切换、订单净额合并、自动资金再分配或权限系统。

## 2. 领域对象与关系

核心关系为：

```text
Strategy Manager
  └─ StrategyRelease 1 ─── N VirtualAccount

FutuChannel 1 ─── N VirtualAccount
  └─ BrokerSimulationAccount 1

VirtualAccount 1 ─── N Decision
Decision 1 ─── N OrderIntent
OrderIntent 1 ─── 1 Order
Order 1 ─── N Fill
VirtualAccount 1 ─── N LedgerEntry / AccountSnapshot
```

领域不变量：

- 一个虚拟账户永久绑定一个`strategy_id + version + release_hash`；
- 一个虚拟账户永久绑定一个`channel_id`，当前只能是`futu`；
- 一个Futu渠道可以承载多个虚拟账户；
- 渠道不绑定策略，也不生成决策；
- 决策必须属于一个账户；
- 订单和成交必须同时属于一个账户和Futu渠道；
- Futu模拟账户由PTE独占，无法归属的外部订单、成交或持仓阻塞新单；
- Futu明确成交后才改变逻辑账户现金和持仓；
- 内部OHLC成交模型不进入正式运行链路。

## 3. 策略与账户

冻结策略由SM管理，PTE通过Trader CLI读取并校验，不在PTE中修改。策略发布身份包括
`strategy_id`、`version`、`release_id`、`release_hash`、名称和资格。

虚拟账户是PTE的核心业务主体，字段包括：

- 稳定唯一的`account_id`和中文名称；
- 完整策略发布身份快照；
- `channel_id=futu`；
- 初始分配资金、现金、冻结资金、总资产；
- 持仓数量、平均成本、已实现盈亏和持仓周期目标；
- `RUNNING`、`PAUSED`、`BLOCKED`或`RETIRED`状态；
- 观察起点、最近决策、错误和创建/更新时间。

账户默认分配10万元。全部非停用账户的初始分配资金之和不得超过Futu模拟账户可分配资产。
账户不能借用其他账户额度。策略或渠道变更通过创建新账户表达，避免污染既有观察历史。

## 4. 持久化模型

### 4.1 `virtual_accounts`

保留表名以延续产品术语，增加`channel_id`和明确的`status`。策略身份、渠道身份和初始资金创建后
不可修改。当前余额字段作为快速状态，与账本在同一事务中更新。

### 4.2 `decisions`

新增账户决策表，至少包含：

```text
decision_id, account_id, release_id, release_hash, symbol,
signal_date, valid_session, action, actual_quantity, target_quantity,
cycle_target_quantity, execution_reference_price, payload, generated_at
```

唯一约束为`(account_id, decision_id)`。同一策略在不同账户产生内容相同的决策，也分别保存账户级
记录。

### 4.3 `order_intents`

统一订单意图表，包含`intent_id`、`account_id`、`decision_id`、`channel_id`、方向、数量、限价、
状态、幂等键和时间。唯一约束覆盖幂等键及`(account_id, decision_id, order_sequence)`。

### 4.4 `orders`

统一本地订单与Futu订单映射，包含`order_id`、`intent_id`、`account_id`、`decision_id`、
`channel_id`、`channel_order_id`、标的、方向、数量、价格、状态、累计成交数量、成交均价和时间。

### 4.5 `fills`

统一成交表，包含`fill_id`、账户、订单、决策、渠道、Futu成交身份、方向、数量、价格、费用和时间。
优先使用Futu成交ID幂等；只能获得订单累计成交量时，以订单身份和累计数量派生确定性身份。

### 4.6 `account_ledger`与`account_snapshots`

账本追加记录每次现金、冻结资金、数量和费用变化以及变化后余额。每日账户快照记录现金、持仓、
市值、总资产和盈亏。账户绩效只依据Futu成交和账户快照计算。

### 4.7 渠道快照

Futu总账户快照保存总资产、可用现金、聚合持仓、活动订单、健康状态和对账时间。它用于容量和
总账校验，不作为任何虚拟账户的业务账本。

## 5. 资金和持仓分账

Futu总账户资金由逻辑账户分配资金与未分配资金构成。账户买单只能使用自己的可用现金；卖单只能
使用自己的逻辑持仓。不同账户订单分别提交，不跨账户抵消或合并。

对账不变量为：

```text
Futu标的总持仓 = 全部逻辑账户该标的持仓合计

Futu现金及订单占用
  = 未分配资金
  + 全部账户现金
  + 全部账户冻结资金
```

持仓数量要求精确相等。资金允许明确配置的小额费用和舍入容差。超出容差、无法识别的Futu活动
订单、成交或持仓均阻塞所有新单，但已有订单继续对账。

## 6. 运行时序

每个交易日19:00后，Scheduler发布并校验完整收盘数据。成功后遍历所有有效账户，使用各账户绑定
策略和自身现金持仓调用Trader advice，保存信号日期为当日、有效日为下一交易日的账户决策。

`BUY`或`SELL`决策先校验账户状态、策略身份、价格档位、整手、账户资金或持仓，再冻结账户资源并
持久化订单意图。下一有效交易窗口中，PTE先完成Futu总账对账，再按稳定账户顺序逐笔提交意图。
订单数量原样使用账户决策结果。

Futu回报通过渠道订单号和本地映射定位账户。在一个SQLite事务中追加成交、更新订单、账户余额、
持仓、成本、账本和审计事件。没有明确成交数量时不改变账户状态。

账户暂停只阻止新订单，仍生成决策、同步已有订单和接收成交。恢复前必须对账；过期决策不补发。
Futu不可用时仍可发布数据和生成决策，新意图等待恢复，超过有效日后过期。系统不使用OHLC模型
补成交，也不自动切换执行方式。

## 7. 幂等、恢复与Futu备注

每个Futu订单备注写入短小可恢复的意图键，例如`PTE:<account-key>:<intent-key>`，完整账户、决策和
订单关系保存在SQLite中。提交前持久化意图，接口超时后先查询备注再决定是否重试。

重启时依次加载账户、决策、意图、订单和账本，查询Futu总账户、订单及成交，通过渠道订单号和
备注恢复归属，补记停机期间成交，校验总账后恢复运行。任何已有意图都不能盲目重复提交。

## 8. 模块边界

```text
paper_trading_engine/
  account_engine.py     账户决策、资源校验和订单意图
  futu_execution.py     多账户订单提交、回报归属和总账对账
  futu_gateway.py       Futu OpenAPI原始适配
  scheduler.py          数据发布、账户遍历和交易时钟
  coordinator.py        CLI/Web共享运行门面
  store.py              SQLite事务、迁移和查询
  audit.py              追加式统一审计事件
  web_api.py            账户、渠道、比较和审计只读模型
  web.py                HTTP路由与控制请求
```

移除`channel_binding.py`、`virtual_engine.py`和`virtual_fill.py`。原`engine.py`中的Futu单策略执行职责
由`account_engine.py`与`futu_execution.py`替代；`coordinator.py`不再组合两套交易引擎。

Trader继续只通过CLI向PTE提供数据发布和账户级advice，PTE不导入Trader内部代码。SM继续拥有策略
发布和资格，PTE只验证并引用冻结发布。

## 9. CLI与HTTP契约

账户创建命令保留`pte account create`，必须给出策略版本；渠道默认且只能为`futu`。删除
`pte channel bind-strategy`。首版不提供账户换策略、换渠道、删除和重置。

HTTP保持资源级边界：

```text
GET  /api/system/status
GET  /api/virtual-accounts
GET  /api/virtual-accounts/{account_id}/snapshot
POST /api/virtual-accounts/{account_id}/pause
POST /api/virtual-accounts/{account_id}/resume
GET  /api/channels/futu/snapshot
POST /api/channels/futu/pause
POST /api/channels/futu/resume
POST /api/channels/futu/cancel-token
POST /api/channels/futu/cancel
GET  /api/comparison
GET  /api/audit-events
```

账户快照只返回该账户的策略、决策、订单、成交、账本摘要、绩效和告警。Futu渠道快照返回渠道健康、
Futu总账户、未分配资金、所承载账户列表以及带账户身份的渠道订单和成交，不返回“渠道决策”或
“当前执行策略”。

撤单请求必须携带`account_id + channel_order_id`，服务端校验订单归属后签发二次确认令牌。

## 10. 控制台信息架构

一级页面保持：

1. `虚拟账户`：账户列表及所选账户的资金、持仓、最新决策、订单、成交、绩效和账户事件；
2. `Futu渠道`：连接、行情、总账户资产、资金分配、承载账户列表、全部渠道订单与成交、渠道告警；
3. `账户比较`：共同观察区间内的最大回撤、卡玛比率、盈亏比和收益率；
4. `审计事件`：全局筛选和因果链查看。

删除Futu页的“当前执行策略”和“最新渠道决策”。Futu订单与成交列表必须显示所属虚拟账户。审计
页面使用“虚拟账户绑定策略”“虚拟账户绑定渠道”等准确名称。

## 11. 审计语义

删除新写入类型`CHANNEL_STRATEGY_BOUND`，新增：

- `ACCOUNT_STRATEGY_BOUND`：账户创建时固化策略发布；
- `ACCOUNT_CHANNEL_BOUND`：账户创建时绑定Futu渠道；
- `ACCOUNT_RECONCILIATION_FAILED/RECOVERED`：账户分账异常及恢复；
- `CHANNEL_RECONCILIATION_FAILED/RECOVERED`：Futu总账异常及恢复。

数据发布和服务生命周期属于系统作用域；决策与信号必须有账户；订单与成交同时有账户和Futu渠道；
Futu接口事件以渠道为主，能够定位订单时补充账户。

历史`CHANNEL_STRATEGY_BOUND`事件保留原始事实并在展示层标记为“历史废弃关系”，禁止改写历史。

## 12. 故障与安全

- 单账户advice或身份校验失败只阻塞该账户；
- Futu连接或总账对账失败阻止全部新单；
- 审计账本不可写时阻止下单、撤单等变更动作；
- 账户逻辑资金或持仓不足只拒绝该账户订单；
- 总渠道资金不足拒绝尚未提交的相关意图，不从其他账户借用；
- 无法归属的Futu订单、成交或持仓视为外部活动并阻塞渠道；
- 暂停不撤销活动订单，撤单继续要求二次确认；
- Futu环境继续硬锁`TrdEnv.SIMULATE`和中国市场；
- watchdog只负责PTE进程保活，不参与交易状态恢复。

## 13. 迁移

上线前正常停止PTE并备份运行库。迁移在SQLite事务内执行：

1. 验证现有Futu及内部模型订单、意图和成交均为空；非空则停止迁移并要求人工方案；
2. 为两个现有虚拟账户增加`channel_id=futu`和新状态字段；
3. 建立决策、统一意图、订单、成交、账本和账户快照表及唯一索引；
4. 保留59,618条旧Futu运行快照和全部历史审计事件；
5. 将旧`CHANNEL_STRATEGY_BOUND`展示为历史废弃关系；
6. 删除`futu_strategy_binding`设置；
7. 删除已验证为空的内部模型意图、订单、成交和快照表；
8. 清除仅服务旧双引擎的运行游标，保留策略和数据日期事实；
9. 记录一次架构迁移审计事件；
10. 启动后先对账，成功后才允许生成和提交新订单。

迁移失败必须完整回滚，不启动半迁移运行时。

## 14. 验收标准

- 代码、数据库、API和页面均不存在可运行的内部virtual渠道；
- Futu渠道不绑定策略，可以承载两个及以上虚拟账户；
- 每个账户只绑定一份冻结策略和Futu渠道；
- 同一次数据发布为所有运行账户生成各自决策；
- 每个Futu订单和成交均能追溯到账户、决策和策略；
- 不同账户使用自身10万元额度，订单数量不按Futu总资产放大；
- 多账户订单不净额合并，成交只更新所属账户；
- Futu未知活动或总账差异能够阻止新单并展示告警；
- 重启不会重复提交意图，停机期间成交可以补记；
- 暂停账户仍对账已有订单，且不会生成新订单；
- Futu页展示账户承载关系和带账户归属的订单成交，不展示渠道策略或渠道决策；
- 历史审计和渠道快照保留，空的内部模型表安全移除；
- PTE功能测试、前端状态测试、Python编译、差异检查及8080运行验证通过。

