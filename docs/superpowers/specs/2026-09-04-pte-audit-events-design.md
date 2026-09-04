# PTE统一审计事件设计

> 2026-09-04补充：运行对象关系以`2026-09-04-pte-account-centric-execution-design.md`
> 为准。策略与决策归属虚拟账户；Futu仅作为共享执行渠道。下文的历史
> `CHANNEL_STRATEGY_BOUND`只用于解释迁移前记录，新运行不再产生该事件。

## 1. 目标

PTE运行时使用一套追加式事件账本记录策略、交易、系统和其他四类事实，使操作者能够回答：

- 哪份数据、哪个策略版本在何时生成了什么决策和信号；
- 哪个虚拟账户或交易渠道依据哪个决策发出了什么订单；
- 订单何时撤销、部分成交或全部成交；
- Futu、Tushare和Trader CLI等依赖在关键链路中是否调用成功；
- 人工操作、服务重启、故障、降级和恢复发生在何时。

首版聚焦单机SQLite和现有控制台，不建设分布式追踪、日志平台、消息总线或自动归档服务。

## 2. 核心原则

事件是已经发生、具有审计价值且不可修改的事实。事件类别由事实的业务含义决定，来源组件、
操作者和执行结果作为独立字段。

- 事件只能追加；纠错通过追加新事件表达。
- 策略决策、交易动作和关键外部调用保持明确区分。
- 订单提交不等于成交，接口成功不等于业务发布成功。
- 重复成功轮询属于运行日志，不写入审计账本。
- 敏感信息在写入边界脱敏，Token、密码和完整凭证不得进入事件详情。
- 所有持久化时间使用UTC ISO 8601，控制台统一显示北京时间。

## 3. 实现方案

原位升级现有`events`表，保留`created_at`、`event_type`和`payload`兼容字段，增加统一审计字段。
已知历史事件按映射表回填；无法可靠识别的历史事件归入`OTHER`。所有新业务代码通过
`AuditRecorder`写入，`PaperStore.add_event`保留为兼容入口并按事件字典补全字段。

数据库继续用`payload`列保存JSON；领域模型和HTTP接口将其命名为`details`，不重复存储两份JSON。

该方案维持一套账本和一套查询接口，避免新旧事件双轨运行。

## 4. 统一事件契约

### 4.1 固定字段

| 字段 | 约束 | 含义 |
|---|---|---|
| `id` | SQLite自增主键 | 本机稳定排序 |
| `event_id` | 唯一、非空 | UUID格式的事件身份；历史记录使用确定性UUIDv5 |
| `occurred_at` | 非空 | UTC ISO 8601时间 |
| `category` | 非空 | `STRATEGY`、`TRADING`、`SYSTEM`、`OTHER` |
| `event_type` | 非空 | 事件字典中的稳定英文代码 |
| `severity` | 非空 | `INFO`、`WARNING`、`ERROR`、`CRITICAL` |
| `outcome` | 非空 | `SUCCESS`、`FAILURE`、`REJECTED`、`SKIPPED`、`UNKNOWN` |
| `source` | 非空 | 产生事实的组件 |
| `correlation_id` | 非空 | 串联同一业务链路；独立事件使用自身`event_id` |
| `actor_type` | 非空 | `ENGINE`、`SCHEDULER`、`OPERATOR`、`EXTERNAL` |
| `actor_id` | 可空 | 操作者、服务或进程实例标识 |
| `schema_version` | 非空 | 首版为`audit.v1` |
| `details` | 非空JSON对象 | 分类相关详情；空详情写`{}` |

### 4.2 可查询作用域

以下字段均可空，但存在对应身份时必须填写：

- `account_id`
- `strategy_id`
- `strategy_version`
- `release_hash`
- `symbol`
- `channel`
- `decision_id`
- `order_id`

索引覆盖`occurred_at`、`category + occurred_at`、`account_id + occurred_at`、
`strategy_id + strategy_version + occurred_at`、`decision_id`和`order_id`。

### 4.3 OTHER约束

`OTHER`仅用于历史未知事件或尚未进入字典的新事实。新写入的`OTHER`事件必须在
`details.classification_reason`中说明原因。已进入正式字典的事件不得写为`OTHER`。

## 5. 事件字典

### 5.1 策略事件 STRATEGY

| 事件类型 | 触发时点 | 关键详情 |
|---|---|---|
| `MARKET_DATA_PUBLICATION_REQUESTED` | 到达发布时间并开始发布 | 目标日期、数据源 |
| `MARKET_DATA_PUBLISHED` | 数据准备、校验和发布全部完成 | 截止日期、数据身份、结果摘要 |
| `MARKET_DATA_PUBLICATION_FAILED` | 发布流程失败 | 阶段、错误类型、脱敏错误摘要 |
| `DECISION_GENERATED` | 获得并验证一份新的决策 | action、目标仓位、实际仓位、参考价 |
| `DECISION_GENERATION_FAILED` | Trader CLI或决策契约失败 | 阶段、错误类型、脱敏错误摘要 |
| `SIGNAL_TRIGGERED` | 新决策首次产生BUY或SELL动作 | side、数量、目标仓位、有效交易日 |
| `SIGNAL_CLEARED` | 原交易信号变为WAIT且目标仓位稳定 | 前一动作、当前目标仓位 |
| `DECISION_EXPIRED` | 决策已不在有效交易日且尚未提交 | 有效交易日、阻止原因 |
| `ACCOUNT_STRATEGY_BOUND` | 虚拟账户绑定不可变策略发布 | 账户、策略、版本、发布哈希 |

相同账户、相同`decision_id`的`DECISION_GENERATED`和`SIGNAL_TRIGGERED`只能各记录一次。
同一数据发布对应的决策只生成一次；缓存命中和相同阻止原因不重复写事件。

### 5.2 交易事件 TRADING

| 事件类型 | 触发时点 | 关键详情 |
|---|---|---|
| `ORDER_INTENT_CREATED` | 本地订单意图在外部调用前持久化 | side、数量、限价、订单类型 |
| `ORDER_INTENT_RECOVERED` | 重启后恢复未完成意图 | 原创建时间、恢复原因 |
| `ORDER_SUBMISSION_BLOCKED` | 新信号因安全规则无法提交且原因发生变化 | 原因代码、活动订单 |
| `ORDER_SUBMITTED` | Futu接受账户订单 | side、数量、限价、渠道状态 |
| `ORDER_SUBMISSION_FAILED` | 提交调用失败或被渠道拒绝 | side、数量、错误类型、错误摘要 |
| `CANCEL_REQUESTED` | 操作者完成二次确认并发起撤单 | 订单当前状态、操作者 |
| `CANCEL_SUCCEEDED` | 渠道确认撤单成功 | 渠道最终状态 |
| `CANCEL_FAILED` | 撤单调用失败或被拒绝 | 错误类型、错误摘要 |
| `ORDER_PARTIALLY_FILLED` | 累计成交数量单调增加但未全部成交 | 本次增量、累计数量、均价 |
| `ORDER_FILLED` | 订单首次达到全部成交 | 本次增量、总数量、均价 |
| `ORDER_TERMINATED` | 订单撤销、失效或终止 | 最终状态、累计成交数量 |

买卖方向统一放在`details.side`，避免维护`BUY_ORDER_SUBMITTED`和
`SELL_ORDER_SUBMITTED`两套平行类型。Futu与虚拟账户使用同一字典，通过`channel`和
`account_id`区分。

### 5.3 系统事件 SYSTEM

| 事件类型 | 触发时点 | 关键详情 |
|---|---|---|
| `SERVICE_STARTED` | PTE实例完成初始化 | instance_id、版本、端口 |
| `SERVICE_STOPPED` | PTE正常退出 | instance_id、原因 |
| `RESTART_REQUESTED` | 接受合法重启请求 | instance_id、操作者 |
| `ACCOUNT_PAUSED` | 渠道或虚拟账户暂停 | 作用域、操作者 |
| `ACCOUNT_RESUMED` | 对账成功后恢复 | 作用域、操作者 |
| `EXTERNAL_CALL_SUCCEEDED` | 关键外部调用成功 | service、operation、duration_ms、摘要 |
| `EXTERNAL_CALL_FAILED` | 外部调用失败 | service、operation、duration_ms、错误摘要 |
| `DEPENDENCY_DEGRADED` | 依赖由健康变为降级或不可用 | service、旧状态、新状态 |
| `DEPENDENCY_RECOVERED` | 依赖恢复健康 | service、旧状态、新状态 |
| `SCHEDULER_OPERATION_FAILED` | 调度任务首次失败 | operation、退避时间、错误摘要 |
| `SCHEDULER_OPERATION_RECOVERED` | 调度任务恢复 | operation、累计失败次数、故障区间 |
| `SCHEDULER_CYCLE_FAILED` | 调度循环自身异常 | 错误类型、错误摘要 |
| `VIRTUAL_ACCOUNT_FAILED` | 单个虚拟账户运行失败 | account_id、错误摘要 |

关键外部调用包括Trader数据发布、Trader决策生成、Futu下单和Futu撤单。Futu账户、持仓、
订单的周期性只读查询仅在失败、恢复或健康状态变化时记录，日常成功轮询不记录。

### 5.4 其他事件 OTHER

历史未知类型迁移为`LEGACY_EVENT`；新的未知事实使用`UNCLASSIFIED_EVENT`并强制填写分类原因。
控制台对`OTHER`显示醒目标识，便于后续评审并升级到正式字典。

## 6. 关联规则

- 数据发布链路使用一次发布任务ID作为`correlation_id`。
- 决策链路优先使用`decision_id`作为`correlation_id`。
- 订单、撤单和成交沿用订单所属决策的`correlation_id`。
- 无决策来源的历史或人工订单使用`order_id`。
- 调度故障与恢复使用`operation + 首次故障时间`派生的关联ID。
- 账户、策略、发布哈希、标的和渠道从决策或绑定快照复制到事件，后续身份变化不回写历史事件。

## 7. 组件与边界

新增`audit.py`：

- `AuditCategory`、`AuditSeverity`、`AuditOutcome`枚举；
- `AuditEvent`不可变数据模型及字段校验；
- 事件类型字典与默认分类、级别、来源规则；
- `AuditRecorder.record()`统一写入、脱敏和兼容映射。

`PaperStore`负责：

- 原位迁移、历史回填和索引；
- 追加单条事件；
- 使用结构化过滤条件分页查询；
- 拒绝更新和删除事件。

Scheduler、Advice Client、Account Engine、Futu Execution、Futu Gateway、Coordinator、Web控制接口只负责
在明确的业务边界调用Recorder，不自行拼装数据库记录。外部适配器通过可选Recorder注入保持
独立测试能力。

## 8. 写入时序与故障处理

交易动作沿用现有先意图、后外部调用原则：

1. 在同一SQLite事务中持久化订单意图和`ORDER_INTENT_CREATED`；
2. 调用Futu；
3. 在同一事务中绑定渠道订单并记录`ORDER_SUBMITTED`；
4. 如果进程在第2、3步之间退出，重启后通过订单remark对账并记录`ORDER_INTENT_RECOVERED`。

审计账本不可写时：

- 尚未发出的下单、撤单等变更性动作立即停止，按安全失败处理；
- 已经由外部系统接受的动作依靠意图和渠道对账恢复，禁止盲目重复提交；
- 只读轮询继续运行并向运行日志输出错误，恢复SQLite后记录系统恢复事件。

单个事件详情序列化失败时拒绝写入该事件并暴露明确错误，禁止静默丢字段。

## 9. 查询接口与控制台

新增只读接口：

`GET /api/audit-events`

支持参数：

- `category`
- `event_type`
- `severity`
- `outcome`
- `account_id`
- `strategy_id`
- `channel`
- `decision_id`
- `order_id`
- `before_id`
- `limit`，默认50、最大200

返回倒序事件列表和下一页游标。查询不接受任意SQL、通配表达式或自由字段排序。

控制台新增“审计事件”全局页面：

- 顶部显示四类事件数量和错误事件数量；
- 提供类别、结果、账户、策略和渠道过滤；
- 主列表显示北京时间、类别中文名、事件中文名、结果、作用域摘要；
- 展开行显示格式化详情、事件ID和关联ID；
- 点击关联ID查看同一因果链；
- 账户页和Futu页继续显示自身作用域事件，复用统一渲染组件。

首版采用定时只读刷新和分页，不实现全文检索、事件导出或实时推送。

## 10. 兼容迁移

启动`PaperStore`时在现有表上逐列执行幂等迁移。历史事件映射如下：

- `DATA_PUBLISHED` → `STRATEGY / MARKET_DATA_PUBLISHED`
- `DATA_PUBLICATION_FAILED` → `STRATEGY / MARKET_DATA_PUBLICATION_FAILED`
- `ORDER_INTENT_CREATED`、`ORDER_INTENT_RECOVERED`、`ORDER_SUBMITTED`和`CANCEL_REQUESTED`
  → 对应`TRADING`类型
- `FILL_INCREMENT` → 能依据订单数量可靠判断时映射为`ORDER_FILLED`，其余映射为
  `ORDER_PARTIALLY_FILLED`
- `CHANNEL_STRATEGY_BOUND` → `STRATEGY / CHANNEL_STRATEGY_BOUND`
- `PAUSED`、`RESUMED`、`PTE_RESTART_REQUESTED`、调度与渠道故障 → 对应`SYSTEM`类型
- 其他类型 → `OTHER / LEGACY_EVENT`，原类型保存在`details.legacy_event_type`

历史`payload`原样保留并作为`details`。历史`event_id`由固定命名空间与原始ID、时间、类型生成
确定性UUIDv5。能够从payload提取的作用域进行回填；无法可靠推断的字段保持空值。迁移完成写入
设置项`audit_schema_version=audit.v1`，重复启动不重复迁移。

## 11. 测试与验收

保持收敛后的端到端测试原则，在现有7个PTE Python功能场景和1个前端场景中扩展覆盖：

- 旧数据库迁移后历史事件数量和payload不变，分类映射正确且迁移幂等；
- 数据发布、决策、信号、订单、成交和接口事件共享正确关联ID；
- 相同决策与相同健康状态不会产生重复事件；
- Futu变更性调用逐次记录，只读成功轮询不会制造审计噪声；
- 虚拟账户和Futu事件作用域不会串户；
- `OTHER`缺少分类原因时被拒绝；
- 敏感字段被拒绝或脱敏；
- 查询过滤、游标、上限和北京时间渲染正确；
- 审计不可写时阻止尚未发出的交易动作；
- 四模块20个Python功能场景、PTE前端场景、56份实验档案和静态检查继续通过。

验收后更新PTE README、根README和开发交接文档，说明事件语义、查询入口、时间口径和故障行为。
