# Strategy Manager 设计

## 1. 背景

当前项目已经通过实验档案、规则基线、Trader advice、PTE虚拟账户和运行数据库，
形成了策略从研究到模拟盘的基本链路。但“候选143”“冠军策略”
“baseline_20260903”“baseline-143”等名称分别表达研究来源、冻结配置和运行账户，
缺少统一的策略身份和生命周期模型。

Strategy Manager（SM）用于回答五个问题：

1. 这是什么策略；
2. 当前运行的是哪一个不可变版本；
3. 该版本具备模拟盘还是实盘资格；
4. 资格变化基于什么证据、由谁决定；
5. 研究、模拟盘和实盘分别取得了什么绩效。

SM面向一个人的OPC团队设计。它必须可审计、可扩展，同时保持单仓库、单操作者、
低运维成本。

## 2. 目标与非目标

### 2.1 目标

- 以稳定ID和正式名称统一标识交易策略；
- 支持同一策略的多个不可变版本；
- 以简单、明确的资格状态贯穿研究、模拟盘和实盘；
- 将研究候选、冻结版本、运行账户和绩效证据分层；
- 记录冻结、晋升、降级和退役的原因及证据；
- 为Trader、PTE和未来实盘执行模块提供稳定契约；
- 保留当前实验档案、基线身份和PTE运行记录的可追溯性。

### 2.2 非目标

首版不建设：

- 独立SM服务、端口、Web应用或常驻进程；
- 多人审批、RBAC、组织架构和电子签名；
- 自动晋升评分或硬编码绩效门槛；
- 自动启动、暂停或停止模拟盘和实盘实例；
- 策略组合、资金组合、依赖图和组合级风控；
- 每日绩效写入Git；
- 通用工作流引擎或消息总线。

## 3. 架构定位

SM采用与`dataflows`相似的定位：同仓库独立Python包，由Trader隐藏式使用。

```text
研究实验 / 回测
      ↓
CZSC Trader ──调用──> Strategy Manager
      │                  ├─策略身份与版本
      │                  ├─资格状态与审计
      │                  └─绩效证据契约
      ↓ CLI / JSON
PTE模拟执行
      ↓ 同一策略契约
未来实盘执行模块
```

依赖规则：

- `strategy_manager`不依赖Trader、PTE、券商SDK和研究运行时；
- Trader依赖`strategy_manager`并提供全部面向用户的管理命令；
- PTE不导入`strategy_manager`，只消费Trader输出的机器契约；
- 未来实盘执行模块遵循与PTE相同的依赖方向；
- SM不读取或修改PTE运行数据库。

## 4. 核心领域模型

首版只包含四个核心对象。

### 4.1 Strategy

`Strategy`表示长期稳定的策略身份，不表示某次参数冻结或某个账户。

必需字段：

| 字段 | 规则 |
| --- | --- |
| `schema_version` | 首版固定为1 |
| `strategy_id` | `S`加三位数字，例如`S001`；创建后永久不变 |
| `name` | 中文正式名称；活动策略之间归一化后不得重名 |
| `objective` | 策略要解决的问题 |
| `responsibility` | 信号、持仓及退出职责边界 |
| `scope` | 标的或适用范围 |
| `created_at` | 带时区的ISO 8601时间 |
| `created_by` | 操作者标识 |

策略名称可以修改，但必须追加审计事件。所有关联关系使用`strategy_id`，不得依赖名称。

### 4.2 StrategyVersion

`StrategyVersion`表示一条策略演化线上的具体版本。

| 字段 | 规则 |
| --- | --- |
| `strategy_id` | 所属策略ID |
| `version` | `v1`、`v2`等单调递增版本 |
| `release_id` | 由两者确定，例如`S001-v1` |
| `parent_version` | 首版为空；后续版本指向直接父版本 |
| `change_summary` | 相对父版本的实质变化 |
| `source_experiment` | 主要来源实验目录 |
| `source_candidate` | 可选的研究候选编号，仅用于来源追溯 |
| `selection_data_cutoff` | 选择与调参可见数据截止日 |
| `forward_start` | 前瞻观察起点 |
| `strategy_payload` | 完整信号、执行、费用和标的约束 |
| `release_hash` | `RESEARCH`时为空；冻结时写入规范化版本内容的SHA-256 |

版本在`RESEARCH`阶段允许调整。执行冻结后写入`release_hash`并进入
`PAPER_READY`，此后版本内容不可修改。任何影响决策、订单、费用或持仓结果的变化，
必须创建下一个版本；UI、日志、watchdog等运行基础设施变化不升级策略版本。

同一策略可以同时存在不同资格的版本，例如`v1`处于实盘资格，`v2`处于模拟盘资格。

### 4.3 LifecycleEvent

生命周期事件只描述策略版本的治理资格，不描述运行状态。

必需字段：

- `event_id`：全局唯一；
- `event_type`：创建、冻结、晋升、降级、退役或重命名；
- `strategy_id`和可选`version`；
- `from_state`和`to_state`；
- `occurred_at`和`actor`；
- 非空`reason`；
- `evidence_ids`；
- 事件涉及冻结版本时保存`release_hash`。

事件采用JSON Lines追加存储。Git历史和事件内的版本哈希共同形成首版审计边界，
不额外实现哈希链或签名系统。

### 4.4 PerformanceEvidence

`PerformanceEvidence`是一次标准化绩效快照及其原始证据引用，不代替研究报告、
订单账本或净值序列。

必需字段：

| 字段 | 说明 |
| --- | --- |
| `evidence_id` | 全局唯一 |
| `strategy_id/version/release_hash` | 绩效所属冻结版本 |
| `phase` | `RESEARCH_BACKTEST`、`PAPER_FORWARD`或`LIVE` |
| `period_start/period_end` | 统计区间 |
| `data_identity` | 数据清单或运行数据身份 |
| `initial_capital` | 绩效口径的初始资金 |
| `fee_rate` | 实际采用的费用口径 |
| `maximum_drawdown` | 最大回撤 |
| `calmar_ratio` | 卡玛比率 |
| `win_loss_ratio` | 盈亏比，可为空 |
| `win_loss_ratio_status` | 区分有效、无亏损、无盈利或无闭合交易 |
| `total_return` | 收益率 |
| `sharpe_ratio` | 夏普率，可为空 |
| `closed_trades` | 闭合交易数量 |
| `source_path/source_hash` | 原始机器证据路径与哈希 |
| `recorded_at/recorded_by` | 登记信息 |

Trader负责研究与回测计算，PTE负责模拟盘计算，未来实盘模块负责实盘计算。
SM只校验并登记标准化快照。日常净值保留在各自运行库；冻结、晋升、降级或人工复核时
才登记治理证据，避免Git产生高频运行数据。

## 5. 资格状态与流转

资格状态作用于`StrategyVersion`：

| 状态 | 含义 | 允许新建的运行实例 |
| --- | --- | --- |
| `RESEARCH` | 仍在研究，内容可调整 | 无 |
| `PAPER_READY` | 已冻结，可进入模拟盘 | 模拟盘 |
| `LIVE_READY` | 已人工晋升 | 模拟盘、实盘 |
| `RETIRED` | 停止继续部署 | 无 |

允许的转换：

| 当前状态 | 操作 | 目标状态 | 约束 |
| --- | --- | --- | --- |
| 无 | 创建版本 | `RESEARCH` | 版本号必须为下一个连续编号 |
| `RESEARCH` | 冻结 | `PAPER_READY` | 完整配置、来源和冻结证据校验通过 |
| `RESEARCH` | 放弃 | `RETIRED` | 必须填写原因 |
| `PAPER_READY` | 晋升 | `LIVE_READY` | 至少引用一项模拟盘前瞻证据 |
| `LIVE_READY` | 降级 | `PAPER_READY` | 必须填写原因和相关证据 |
| `PAPER_READY` | 退役 | `RETIRED` | 必须填写原因 |
| `LIVE_READY` | 退役 | `RETIRED` | 必须填写原因 |

禁止：

- 冻结版本回到`RESEARCH`后继续修改；
- `RETIRED`恢复；需要恢复思想时创建新版本；
- 跳过冻结直接进入`LIVE_READY`；
- 用修改历史事件的方式改变当前资格。

冻结、晋升、降级和退役均为人工决策。SM只验证结构、证据存在性和转换合法性，
不内置收益率或其他自动门槛。

## 6. 资格状态与运行状态的边界

SM明确不管理以下状态：

- 自动运行、暂停、停止；
- 正常、降级、异常阻塞；
- 账户连接、行情连接；
- 订单提交、撤单、成交和对账状态。

这些状态属于PTE或未来实盘执行模块。资格变化不自动修改运行实例：

- 降级只禁止新建实盘实例；
- 退役只禁止新建模拟盘和实盘实例；
- 已有实例显示“资格不匹配”告警，由操作者在执行模块中处理；
- 模拟盘可以在版本晋升实盘后继续作为影子账户运行。

## 7. 文件与代码组织

### 7.1 Python包

```text
packages/strategy_manager/
  pyproject.toml
  src/strategy_manager/
    __init__.py
    models.py
    registry.py
    lifecycle.py
    validation.py
    errors.py
  tests/
```

职责：

- `models.py`：四个领域对象及序列化；
- `registry.py`：文件加载、原子写入、查询、ID分配和规范化哈希；
- `lifecycle.py`：状态转换和资格校验；
- `validation.py`：Schema、来源、证据和跨对象一致性；
- `errors.py`：稳定的领域错误类型。

包内不包含CLI、HTTP、SQLite、调度、页面和券商适配器。

### 7.2 Git数据

```text
configs/strategies/
  registry.json
  S001/
    strategy.json
    versions/
      v1.json
    lifecycle.jsonl
    evidence.jsonl
    evidence/
      <evidence_id>.json
```

`registry.json`只保存策略ID、路径和历史别名，不复制完整策略内容。JSON对象使用
规范化语义哈希；JSON Lines事件和证据采用UTF-8、LF和每行一个完整对象。
研究证据可以引用Git已跟踪的实验制品；模拟盘和未来实盘的里程碑证据在登记时复制到
对应策略的`evidence/`目录，避免只留下无法跨机访问的本地运行库路径。

写操作由Trader命令串行执行，采用临时文件加原子替换。首版按单操作者模型处理，
不增加分布式锁；检测到目标文件在操作过程中变化时拒绝覆盖。

## 8. SM包接口

SM提供Python接口，不直接提供命令：

- `list_strategies()`
- `get_strategy(strategy_id)`
- `get_version(strategy_id, version)`
- `resolve_strategy(reference)`：接受正式ID或历史别名；
- `create_strategy(...)`
- `create_version(...)`
- `freeze_version(...)`
- `promote_version(...)`
- `downgrade_version(...)`
- `retire_version(...)`
- `record_evidence(...)`
- `current_qualification(...)`
- `assert_deployable(strategy_id, version, environment)`

所有写操作返回写入对象及新审计事件。领域校验失败不产生部分文件或事件。

## 9. Trader边界

Trader是唯一用户入口，新增：

```text
czsc-trader strategy list
czsc-trader strategy show --strategy S001 [--version v1]
czsc-trader strategy history --strategy S001
czsc-trader strategy create ...
czsc-trader strategy version create ...
czsc-trader strategy freeze ... --reason ... --evidence ...
czsc-trader strategy promote ... --reason ... --evidence ...
czsc-trader strategy downgrade ... --reason ... --evidence ...
czsc-trader strategy retire ... --reason ...
czsc-trader strategy evidence add --input ...
czsc-trader strategy performance --strategy S001 [--version v1]
czsc-trader strategy validate [--all]
```

Trader应用层负责：

- 将研究胜出候选组装成完整版本载荷；
- 调用SM完成冻结和资格变化；
- 将研究回测结果转换为`PerformanceEvidence`；
- 聚合展示SM登记的跨阶段绩效；
- 通过CLI向PTE解析策略身份。

## 10. Trader与执行模块契约

策略身份升级为`advice.v4`：

```json
{
  "contract_version": "advice.v4",
  "strategy": {
    "strategy_id": "S001",
    "name": "综合基线策略",
    "version": "v1",
    "release_id": "S001-v1",
    "release_hash": "...",
    "qualification": "PAPER_READY"
  }
}
```

订单和账户字段延续`advice.v3`已验证的语义。`decision_id`包含`strategy_id`、
`version`和`release_hash`，不包含可变名称或资格状态，避免重命名或资格调整产生重复订单。

迁移完成后：

- 新运行决策只使用`advice.v4`；
- 历史`advice.v3`记录继续可读，不重新解释或改写；
- PTE不自行读取`configs/strategies/`；
- PTE调用Trader并验证返回版本具有模拟盘资格；
- 未来实盘模块验证版本具有实盘资格。

## 11. PTE边界

PTE虚拟账户绑定：

- `strategy_id`
- `strategy_name_snapshot`
- `strategy_version`
- `release_hash`
- 创建时的资格快照
- 内部`account_id`
- 初始资金及运行账本

账户ID继续作为PTE内部技术身份，页面主界面只显示“策略名称 · 版本”。名称快照用于
保留历史展示；当前正式名称可在状态刷新时由Trader提供。

PTE负责：

- 通过Trader验证`PAPER_READY`或`LIVE_READY`；
- 创建和恢复虚拟账户；
- 保存模拟盘订单、成交、净值和运行状态；
- 导出标准化模拟盘绩效证据；
- 在资格降级或退役后显示告警，但不自动停机。

SM不保存PTE账户、部署、暂停状态或逐日净值。

## 12. 绩效证据流

### 12.1 研究与冻结

Trader从不可变实验档案读取机器结果。冻结操作在同一原子流程中计算发布哈希、生成并
登记研究绩效证据、追加冻结事件。冻结输入必须包含：

- 来源实验及其manifest；
- 版本配置哈希；
- 至少一条研究回测证据。

### 12.2 模拟盘与晋升

PTE从指定虚拟账户和共同观察区间导出证据JSON。Trader验证并登记该文件，晋升操作
引用已登记的`PAPER_FORWARD`证据。原始账本仍以PTE SQLite为事实来源。

### 12.3 实盘

未来实盘模块采用相同证据Schema，`phase`为`LIVE`。SM不把模拟和实盘绩效合并成
单一收益曲线，跨阶段只并列展示。

## 13. 当前项目迁移

当前正式策略映射为：

| 新字段 | 当前值 |
| --- | --- |
| `strategy_id` | `S001` |
| `name` | `综合基线策略` |
| `version` | `v1` |
| `release_id` | `S001-v1` |
| `legacy_alias` | `baseline_20260903` |
| `source_experiment` | `0901_EX20` |
| `source_candidate` | `143` |
| `execution_review` | `0903_EX01` |
| `qualification` | `PAPER_READY` |
| 默认虚拟资金 | `100,000.00`元 |

迁移原则：

1. 历史实验和旧基线文件不修改；
2. `S001-v1`完整载荷由`baseline_20260903`确定，并记录原身份和哈希；
3. `configs/strategies/`成为新正式版本的唯一注册表；
4. `baseline_20260903`作为只读历史别名解析到`S001-v1`；
5. `baseline list/show/validate`至少保留至P1验收完成，只读转发到策略入口；后续删除
   必须作为独立兼容性决策；
6. 新冻结操作只产生策略版本，不再产生业务含义上的“基线”；
7. PTE现有`baseline-143`账户ID和账本原样保留，增加策略身份字段；
8. 页面主名称改为“综合基线策略 · v1”，研究候选和内部账户ID进入审计详情；
9. 历史`advice.v3`决策保持原样，新决策一次性切换到`advice.v4`。

## 14. 错误与安全策略

- 注册表、版本、事件或证据校验失败时拒绝整个写操作；
- 冻结后的版本内容哈希不匹配时视为仓库完整性错误；
- 未知状态转换返回稳定领域错误，不自动纠正；
- 缺失证据时不允许冻结或晋升；
- PTE收到策略身份或哈希不匹配时不结算、不生成新订单；
- 资格不满足时禁止创建新运行实例；
- SM写操作不触发交易、账户或进程副作用；
- 所有时间保存带时区ISO 8601值，业务日期单独保存为`YYYY-MM-DD`。

## 15. 实施分期

### P0：身份与只读迁移

- 建立独立`strategy_manager`包；
- 定义四个领域对象和文件Schema；
- 创建`S001-v1`及历史别名；
- 增加Trader策略查询和全量验证命令；
- 保持PTE现有运行行为不变。

### P1：生命周期与契约切换

- 实现创建、冻结、晋升、降级和退役；
- 实现证据登记；
- 发布`advice.v4`；
- 迁移PTE账户身份和页面名称；
- 增加模拟盘绩效证据导出。

### P2：实盘接入准备

- 提供`LIVE_READY`资格校验契约；
- 使用模拟盘证据完成一次人工晋升演练；
- 固化未来实盘模块必须实现的策略身份和绩效证据接口。

P2不实现真实资金下单。

## 16. 验收标准

- 页面及CLI可以用`S001 / 综合基线策略 / v1`唯一说明当前策略；
- 候选编号、历史基线别名和运行账户ID不再承担正式策略名称；
- 冻结版本内容不可修改，哈希变化能够被验证命令发现；
- 非法状态跳转、缺失原因或证据时写操作失败且不产生部分记录；
- `PAPER_READY`不能创建实盘实例，`LIVE_READY`可以创建模拟盘和实盘实例；
- SM资格变化不直接启停任何运行实例；
- Trader是SM唯一用户入口，PTE不导入SM；
- `advice.v4`完整携带策略ID、名称、版本和发布哈希；
- PTE账户按策略版本隔离，历史账本和`advice.v3`记录保持可读；
- 研究、模拟盘和实盘绩效分阶段展示，不拼接为单一收益曲线；
- 当前`baseline_20260903`可追溯到`S001-v1`、EX20候选143和EX01执行复审；
- 新机器仅凭Git跟踪的注册表、事件和证据索引即可恢复策略治理上下文；
- 聚焦测试、Python编译、静态检查和`git diff --check`通过。

## 17. 未来扩展点

稳定ID、版本、资格事件和证据Schema预留以下扩展能力，但首版不实现：

- 多标的或组合策略；
- 自动化晋升政策插件；
- 多账户部署与资金上限规则；
- 独立SM API服务；
- 多人审批与签名；
- 组合级绩效和风险归因。

这些扩展不得改变Trader统一入口和执行模块只消费策略契约的依赖方向。
