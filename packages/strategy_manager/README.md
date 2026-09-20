# 策略管理器（Strategy Manager，SM）

SM 是策略身份、生命周期和治理证据的领域包。它把策略族、研究候选、评审结论、冻结版本和
运行证据保存为可审计的不可变事实，并校验状态转换是否合法。

## 职责与边界

SM 负责：

- 管理长期稳定的 `StrategyFamily` 身份；
- 追加保存 `StrategyGovernanceCredential`（SGC）及其治理封印；
- 保存 `CandidateSnapshot`、`EvaluationMandate`、`AdjudicationReport` 和
  `FreezeApproval`，形成候选冻结的完整证据链；
- 发布不可变的 `StrategyVersion`，记录生命周期事件和阶段性绩效证据；
- 通过原子写入、仓库写锁和并发变更检测保护治理状态。

SM 不获取行情、不运行或评价策略、不计算绩效，也不部署 PTE。TDR 负责组织研究与冻结评审，
SE 负责确定性数值评估，SRT 负责执行候选或冻结策略；SM 只接收这些模块已经形成的结构化事实。

## 治理链路

冻结版本必须依次具备以下事实：

1. 研究批次形成候选快照；
2. 评审锁定评价任务并形成裁决报告；
3. 人工批准后发布冻结版本。

`release_hash` 标识可执行发布内容，`governance_hash` 标识完整治理证据。二者用途不同，均需保持
不可变。后续模拟盘或实盘证据以 `PerformanceEvidence` 追加，不能覆盖既有版本和历史裁决。

## 存储

`StrategyRegistry` 默认把事实写入仓库根目录的 `strategies/`。每个策略族使用独立的
`strategies/SXX/`目录：

- `family.json`：策略族定义；
- `credentials/<credential_id>.jsonl`：追加式 SGC 记录；
- `versions/vN.json`：冻结版本；
- `freeze_approvals.jsonl`：冻结批准记录；
- `lifecycle.jsonl`：生命周期事件；
- `evidence.jsonl`及`evidence/`：阶段性绩效记录和自包含证据制品。

这些文件属于治理账本。人工编辑会破坏哈希或状态链，应通过 SM 接口修改。

## 包级验证

```powershell
.\.venv\Scripts\python.exe -B -m pytest -c pyproject.toml packages/strategy_manager/tests -q
.\.venv\Scripts\python.exe -m ruff check packages/strategy_manager/src packages/strategy_manager/tests
```
