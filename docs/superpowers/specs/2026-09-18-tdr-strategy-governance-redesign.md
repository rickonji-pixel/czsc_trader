# TDR可信策略治理重构设计

## 1. 背景

当前系统已经具备研究实验、候选评估、策略冻结、SRT运行和PTE模拟观察能力，但TDR的职责逐步
扩张为数据、特征、搜索、回测、评估和冻结等能力的集合。现有冻结路径还存在以下结构问题：

- 研究批次成立时没有同步建立正式策略族身份；
- Strategy通常到接受评估时才根据候选材料自动创建；
- 候选送审与正式冻结之间缺少不可变的评审案件；
- 普通CLI仍允许直接创建版本和直接冻结，容易无意绕过正式流程；
- 接受评估、冻结策略和创建PTE账户被一个命令串联，PTE失败时仍可能返回整体成功；
- Search、Feature Mining等研究能力被描述为TDR内部能力，与“可信裁判员和维护人”的定位冲突。

本次重构将TDR定位为：

> 正式策略结论的可信裁判员，以及策略族和冻结版本的维护人。

TDR不负责创造Alpha。研究脚本可以自由选择数据分析库、特征方法、策略原型和搜索算法；只有
进入正式策略生命周期的结论，才必须由TDR独立复核、完整审计并维护不可变版本。

## 2. 设计目标

1. 以三个人工确认节点控制正式策略生命周期；
2. 研究探索保持灵活，正式结论保持真实、完整、可复算和不可漂移；
3. 在立项时建立稳定的StrategyFamily身份；
4. 以一份只追加的SGC贯穿立项、送审、裁判、批准和冻结；
5. 在人工批准后才创建不可变StrategyVersion；
6. 将冻结与PTE部署彻底解耦；
7. 保留现有冻结版本、release hash、SRT绑定和PTE历史身份；
8. 以少量JSON对象、哈希和明确状态机完成治理，不引入通用工作流引擎。

## 3. 非目标

本次重构不建设：

- 多人审批、RBAC或电子签名；
- 独立常驻的TDR或SM服务；
- 自动判断金融逻辑、投资价值或风险偏好；
- 自动把研究目标转成仓库级统一硬门；
- 每轮探索实验的完整稳健性体检；
- 冻结后自动创建、暂停或删除PTE账户；
- 为已经废弃的直接CLI长期维护兼容层；
- Git clone后的历史实验全量回放保证。

## 4. 核心原则

### 4.1 职责归属与计算实现分离

TDR对正式复核和完整体检负责，但具体计算由专业组件完成：

| 领域 | 责任组件 | TDR职责 |
| --- | --- | --- |
| 数据可得性与发布 | DFLS | 校验数据契约和证据身份 |
| 因子与信号定义 | FSC | 读取候选声明，不提供挖掘能力 |
| 策略原型 | STC | 读取模板身份和参数边界 |
| 参数搜索 | Search | 核验搜索记录、抽查代表点和参数邻域 |
| 成交、账本与净值 | 统一成交执行器 | 独立重算账户事实和成本场景 |
| 数值化稳健性审计 | SE | 决定必需体检集合并验证结果完备性 |
| 身份和不可变版本 | SM | 执行正式写操作并维护生命周期 |
| 冻结策略运行 | SRT | 冻结前验证运行时可实现性 |
| 模拟盘 | PTE | 仅在单独授权后创建和运行账户 |

“TDR负责”表示TDR必须保证事项完成且结论可追溯，不表示算法必须实现在TDR包内。

### 4.2 探索自由，晋升受控

实验脚本可以自由导入新库、编写临时算法和生成诊断结果。未进入正式复核的结果只具有研究证据
身份。TDR不设置导入白名单，也不要求每轮实验都先修改基建。

候选进入冻结流程后，目标、候选、数据和执行口径必须锁定。任何实质变化都会使当前评审案件
失效，必须重新送审。

### 4.3 不信任派生指标，独立复核核心事实

研究报告中的收益、回撤、交易数量和稳健性结论属于研究主张。TDR必须从候选快照、标准数据、
统一执行规则和底层账本重算核心指标。研究脚本提供的裸指标不能直接成为冻结证据。

### 4.4 失败即暴露

缺少数据、证据、体检项、哈希或运行实现时，TDR返回明确失败或`INCOMPLETE`。不得以warning、
默认值或自动降级继续冻结。PTE部署属于独立动作，其失败不得改变冻结结果，也不得被包装成部署
成功。

## 5. 领域对象

### 5.1 StrategyFamily

`StrategyFamily`是`SXX`的长期稳定身份，同时承载该策略族的研究过程。人工确认新研究批次后
立即创建。

建议字段：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 对象Schema版本 |
| `strategy_family_id` | 稳定ID，例如`S008` |
| `name` | 中文正式名称 |
| `scope` | 初始标的和适用范围 |
| `research_intent` | 当前方向性研究意图 |
| `research_state` | `RESEARCHING`、`PAUSED`或`TERMINATED` |
| `created_at/created_by` | 立项信息 |
| `updated_at` | 最近一次家族级信息变更时间 |

`research_intent`是可演化的人类语义，不是机器验收合同。变更必须追加治理事件，但不触发自动
绩效判断。一个StrategyFamily可以有多个候选和多个冻结版本；是否已有冻结版本由版本集合派生，
不占用研究状态。已经冻结`v1`的策略族仍可继续研究新的候选和`v2`。

为了控制迁移成本，既有JSON中的`strategy_id`和既有版本引用可以继续保留；Python领域名和新
接口使用`StrategyFamily`。是否迁移持久化字段名留待后续独立决策。

### 5.2 StrategyGovernanceCredential（SGC）

每个经人工确认的研究批次创建一份`StrategyGovernanceCredential`。候选、评价合同、裁判结果、
人工批准和最终版本不再分别维护相互引用的治理对象，而是作为同一份凭据上的连续印章传递。

SGC采用只追加的哈希链。每枚`StrategyGovernanceSeal`至少包含：

| 字段 | 含义 |
| --- | --- |
| `credential_id/strategy_id` | 凭据和策略族身份 |
| `sequence` | 从1开始连续递增的印章序号 |
| `stage/result` | 当前治理阶段及机器结果 |
| `actor/occurred_at` | 盖章主体和时间 |
| `previous_seal_hash` | 前一枚印章哈希；首枚为空 |
| `content/content_hash` | 本阶段固化的规范化内容及哈希 |
| `artifact_hashes` | 外部证据文件的名称和内容哈希 |
| `seal_hash` | 当前印章除自身外全部字段的规范化哈希 |

一份凭据可以依次出现以下印章：

1. `RESEARCH_INITIATED`：固化人工立项事实和方向性研究意图；
2. `CANDIDATE_SUBMITTED`：固化候选、最终评价合同、完整评价输入包、数据与执行契约；
3. `TDR_ADJUDICATED`：固化独立重算、完整体检、证据矩阵和机器裁决；
4. `FREEZE_APPROVED`：固化人工批准人、理由和保留意见；
5. `VERSION_FROZEN`：固化不可变策略版本、SRT验收和release hash。

`INCOMPLETE`或`REJECTED`的裁判印章之后可以在同一研究批次中追加新的候选送审印章；已经
`FROZEN`的凭据终止。新的研究批次必须创建新的SGC。任何阶段也可以追加`INVALIDATED`印章，
保留原有证据而停止当前送审链路。

准确的研究目标在`CANDIDATE_SUBMITTED`时完全确定。该印章中的评价合同至少包含开发截止、
正式窗口、对手、硬目标、成本压力、频率属性、参数与统计要求、技术回放、部署兼容和监测要求。
目标形成时间及其读取过的开发证据边界必须如实记录。

TDR只裁判可计算事实和流程完整性。金融逻辑是否可信、证据是否值得承担资金风险，继续由人工
判断。

### 5.3 StrategyVersion

`StrategyVersion`只在`VERSION_FROZEN`印章原子写入时创建。正式版本只需要绑定：

- StrategyFamily；
- 对应SGC的`credential_id`；
- `VERSION_FROZEN`印章哈希；
- 完整冻结记录哈希；
- SRT可执行身份和release hash。

版本号在冻结时分配，不为尚未冻结的候选预占`vN`。SM必须验证完整SGC哈希链、冻结印章、
版本记录哈希和release hash；任何一处缺失或漂移均不得部署。历史版本继续沿用原有release hash
算法，不重写既有身份。

## 6. 三个人工确认节点

### 6.1 节点一：创建新研究批次

人工确认后，TDR：

1. 分配或校验`SXX`；
2. 创建StrategyFamily；
3. 创建`research/SXX/`和基础交接材料；
4. 记录方向性ResearchIntent；
5. 创建SGC并追加`RESEARCH_INITIATED`印章。

节点一不创建StrategyVersion，不创建PTE账户，不把ResearchIntent转为机器硬门。

### 6.2 节点二：候选进入冻结流程

人工确认后，TDR：

1. 验证SGC此前全部印章；
2. 以`CANDIDATE_SUBMITTED`印章一次性固化候选、最终评价合同、评价输入包和审计策略；
3. 独立复核核心主张；
4. 组织完整体检；
5. 以`TDR_ADJUDICATED`印章固化裁判报告和全部证据哈希；
6. 将机器结果置为`ELIGIBLE`、`INCOMPLETE`或`REJECTED`。

完整体检只在此节点执行，不施加给普通EX实验。

### 6.3 节点三：候选正式冻结

人工阅读裁判报告并批准后，TDR：

1. 验证人工批准人与理由；
2. 验证SGC最新裁判印章为`ELIGIBLE`；
3. 验证整条印章哈希链和全部证据身份；
4. 验证SRT运行时可实现性；
5. 追加`FREEZE_APPROVED`印章；
6. 原子创建StrategyVersion并追加`VERSION_FROZEN`印章；
7. 由SM持久化版本、资格和生命周期事件。

该节点不调用PTE。进入模拟盘必须通过单独的PTE账户创建流程和独立授权。

## 7. TDR、SM和SE边界

### 7.1 TDR

TDR是唯一正式治理入口，负责：

- 创建和维护StrategyFamily；
- 驱动SGC逐层盖章；
- 读取研究主张并独立复核；
- 决定完整体检清单并调用专业组件；
- 验证体检无遗漏、无口径漂移且可复算；
- 生成并固化裁判印章；
- 在人工批准后执行原子冻结；
- 查询、比较、退役和替代冻结版本。

### 7.2 SM

SM是无CLI的领域包和持久化边界，负责：

- StrategyFamily、StrategyVersion、SGC印章和LifecycleEvent模型；
- SGC哈希链及正式证据的结构校验和存储；
- ID唯一性、版本连续性、原子写入和哈希；
- 生命周期状态转换和不可变约束。

SM不计算收益，不运行SE，不调用PTE，也不决定金融价值。

### 7.3 SE

SE是确定性数值评估包，负责：

- 按结构化输入计算筛选、排名和稳健性统计；
- 返回PBO、DSR、Bootstrap、邻域等机器结果；
- 校验自身输入Schema和数值一致性。

SE不加载市场数据，不管理StrategyFamily、SGC或StrategyVersion，不签发人工冻结决议。TDR
负责判断SGC评价合同要求的体检项是否全部由SE及其他组件完成。

## 8. 命令边界

建议的正式用户入口：

```text
czsc-trader research create ...
czsc-trader strategy review open ...
czsc-trader strategy review evaluate ...
czsc-trader strategy review show ...
czsc-trader strategy freeze ...
czsc-trader strategy list/show/history/validate ...
czsc-trader strategy retire/supersede ...
```

退出普通用户工作流：

```text
strategy create
strategy version create
直接传入任意evidence和health-check的旧strategy freeze
一次性完成评估、冻结和PTE激活的accept-evaluation
```

旧创建和冻结能力只能存在于一次性迁移适配器，并受固定历史版本清单约束；正式SM API不得提供
绕过SGC印章链的入口。

## 9. 持久化建议

继续使用Git管理的JSON和JSON Lines：

```text
strategies/S008/
  family.json
  lifecycle.jsonl
  credentials/
    SGC-S008-001.jsonl
  versions/
    v1.json
  evidence.jsonl
```

大型净值、订单、成交和搜索明细仍保存在实验制品或本地数据区；正式对象只保存引用、哈希和必要
摘要。仓库公开后不提交私密数据、密钥和大体积可再生产物。

## 10. 假成功防线

1. 研究主张与独立重算不一致时返回`CLAIM_MISMATCH`；
2. 任一强制体检缺失时裁判印章只能为`INCOMPLETE`；
3. 任一历史印章、候选、目标、审计策略或证据哈希变化时整条SGC失效；
4. SRT运行时验收失败时不创建StrategyVersion；
5. SM写入必须原子化，失败不留下版本号或生命周期事件；
6. PTE部署单独返回结果，失败不得改变策略冻结状态；
7. 重复执行只在所有输入和输出身份完全一致时返回幂等成功。

## 11. 历史迁移原则

- S001至S007迁移为StrategyFamily，保留现有`strategy_id`；
- 已冻结版本、release hash、候选来源和SRT绑定保持不变；
- 不重写历史实验和SE报告；
- 从现有研究交接材料提取家族级ResearchIntent，只作为当前摘要；
- 现有`PAPER_READY`版本补充关联迁移记录，不伪造历史SGC印章链；
- 历史版本标记为`LEGACY_GOVERNANCE_ACCEPTED`，表示按当时流程冻结，而非重新宣称通过新流程；
- 新流程启用后，所有新候选必须经过三个节点。

## 12. 验收标准

重构完成后必须证明：

1. 人工立项能够创建StrategyFamily和研究空间；
2. 普通实验不承担完整体检成本；
3. SGC从立项到冻结保持单链传递，任一印章不可修改或跳过；
4. TDR能独立重算核心主张并发现错报；
5. 缺少任一强制体检时无法获得冻结资格；
6. 修改受审候选或目标后无法使用旧报告冻结；
7. 正式冻结原子创建StrategyVersion；
8. 冻结动作不调用PTE；
9. PTE部署失败不会产生成功状态；
10. 现有五个冻结版本继续被SRT和PTE正确识别；
11. 旧的直接入口无法从普通CLI绕过治理；
12. 文档中的TDR、SM、SE、SRT和PTE边界与代码一致。
