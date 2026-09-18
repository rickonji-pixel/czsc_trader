# TDR可信策略治理重构实施计划

## 总体节奏

本次重构采用分阶段交付，每阶段完成最小验证后暂停评审。后一阶段不得提前改变前一阶段尚未
确认的对象或契约。

## 阶段一：设计冻结

交付：

- `docs/superpowers/specs/2026-09-18-tdr-strategy-governance-redesign.md`；
- 本实施计划；
- 现有行为与目标行为的差异清单。

评审重点：对象关系、三个人工节点、TDR/SM/SE边界、PTE解耦和历史迁移原则。

验证：文档内部一致性和仓库相对路径检查。

## 阶段二：SM领域模型和历史迁移

交付：

- 将Python领域对象`Strategy`明确为`StrategyFamily`；
- 增加ResearchIntent摘要、研究状态和家族级治理事件；
- 增加CandidateSnapshot、EvaluationMandate和FreezeReviewCase模型；
- 增加AdjudicationReport引用及哈希校验；
- 为S001至S007生成无损迁移；
- 保持既有版本文件、release hash和SRT绑定不变。

最小验证：

- SM功能测试；
- 历史五个冻结版本身份和hash回归；
- 迁移前后SRT加载结果一致。

## 阶段三：节点一与研究控制入口

交付：

- 新增`research create`；
- 原子创建StrategyFamily和`research/SXX/`；
- 支持ResearchIntent的人工修订和事件记录；
- Search、Feature Mining等能力不再通过TDR包装成研究能力入口。

最小验证：

- 新批次创建成功与重复ID拒绝；
- 目录或SM任一写入失败时不留下半创建状态；
- 实验脚本可以自由导入第三方库。

## 阶段四：节点二与可信裁判链路

交付：

- `strategy review open/evaluate/show`；
- 固化CandidateSnapshot、EvaluationMandate和AuditPolicy；
- 以ReviewCase驱动正式评价，移除公开的旧`strategy evaluate`入口；
- 独立复核核心目标；
- 完整体检矩阵和遗漏阻断；
- 生成与案件绑定的AdjudicationReport；
- 候选或目标漂移后案件自动失效。

最小验证：

- 收益错报产生`CLAIM_MISMATCH`；
- 缺少体检项产生`INCOMPLETE`；
- 修改候选、目标或审计策略后旧报告失效；
- 普通EX实验无需执行完整体检。

说明：统一成交执行能力由独立的Trading Execution Engine（TXE）包提供，与DFLS同层，避免
把成交实现再次内聚到TDR。

## 阶段五：节点三与原子冻结

交付：

- 以ReviewCase为唯一正式冻结入口；
- 冻结时才分配`vN`并创建StrategyVersion；
- 人工决议、裁判报告、运行时实现和版本内容完整绑定；
- SM原子写入版本、证据和生命周期事件；
- 移除普通CLI中的`strategy create`、`strategy version create`和旧直接冻结路径；
- 废弃`strategy accept-evaluation`。

最小验证：

- 未获资格、缺少人工批准、hash漂移和SRT验收失败均无法冻结；
- 任一失败不产生半版本；
- 重复冻结只有完全同一输入时幂等；
- 现有冻结版本不受影响。

## 阶段六：冻结与PTE部署解耦

交付：

- 删除冻结服务对`pte.exe`的调用；
- PTE账户创建保持独立命令和授权；
- PTE只接受SM中可部署的冻结版本；
- 部署失败返回明确FAIL，不影响SM冻结状态；
- 文档明确“已冻结”和“已进入模拟盘”是两个状态。

最小验证：

- 冻结成功后没有新增PTE账户；
- 未冻结版本无法创建策略账户；
- PTE注册失败无假成功；
- 五个既有账户继续运行。

## 阶段七：边界清理与文档更新

交付：

- 更新`research/README.md`、`docs/DEVELOPMENT_HANDOFF.md`和用户说明；
- 清理TDR内部能力定位和废弃命令；
- 明确Search、Feature Mining、SE、SM、SRT、DFLS和成交执行器的包级边界；
- 更新测试治理清单；
- 清理本轮产生的临时迁移工具。

最小验证：

- CLI帮助与文档一致；
- 仓库无绝对路径和被误跟踪的临时产物；
- 相关模块功能回归通过。

## 阶段八：版本验收

仅在前七阶段全部确认后执行：

- TDR、SM、SE、SRT、DFLS、PTE完整功能回归；
- 现有五个冻结版本完整回放；
- 三个人工节点端到端验收；
- 假成功专项用例；
- Git状态、文档、迁移和公开仓库安全检查。

合并master、打Tag和推送远端均另行取得人工确认。
