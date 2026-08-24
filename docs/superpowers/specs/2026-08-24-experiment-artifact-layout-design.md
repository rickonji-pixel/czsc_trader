# 研究资料目录与输出边界设计

**日期：** 2026-08-24  
**状态：** 待用户审阅  
**适用项目：** `czsc_trader`

## 1. 背景与目标

当前研究资料分散在 `configs/experiments/`、`docs/experiments/`、`docs/superpowers/plans/` 和本机 `outputs/` 中。研究设计、执行证据和结论缺少统一目录，且研究运行产物与普通回测输出共用 `outputs/`，不利于跨设备复现和审计。

本次调整建立两个明确边界：

1. 根目录 `experiments/` 保存每一轮正式研究的完整资料，全部纳入 Git 跟踪。
2. 根目录 `outputs/` 只保存普通固定规则回测的本机输出，继续被 Git 忽略。

## 2. 实验目录编号

每轮研究目录格式固定为：

```text
experiments/MMDD_EXX/
```

- `MMDD` 使用 Asia/Shanghai 时区的运行日期。
- `XX` 为两位序号，从 `01` 开始。
- 序号按日期重置；同一天内单调递增。
- 创建目录时只扫描同一 `MMDD_EX*`，取已有最大序号加一。
- 不填补历史空号，不覆盖已有目录。
- 超过 `EX99` 时明确失败，不自动扩展格式。

示例：

```text
experiments/0824_EX01/
experiments/0824_EX02/
experiments/0825_EX01/
```

## 3. 每轮研究的必需资料

每个实验目录采用“固定四文档、机器证据、完整性清单”的结构：

```text
experiments/0824_EX01/
├── 01_goal.md
├── 02_design.md
├── 03_execution.md
├── 04_conclusion.md
├── experiment_manifest.json
└── artifacts/
    ├── protocol.json
    ├── candidate_results.csv
    ├── champion_metrics.json
    └── metrics.json
```

### 3.1 `01_goal.md`

必须记录：

- 研究背景与问题；
- 单一、可证伪的研究假设；
- 当前冠军基线及其规则哈希；
- 证券代码和资产类型；
- 样本内与样本外边界；
- 明确的 PASS 条件；
- 本轮不研究的内容。

### 3.2 `02_design.md`

必须记录：

- 本轮唯一允许变化的规则维度；
- 完整候选空间及候选数量；
- 训练、验证或滚动窗口；
- 收益率、夏普率等选优指标；
- 候选排序和并列处理方法；
- 费用、初始资金及成交假设；
- 未来数据隔离措施；
- 冻结、停止和样本外开启条件。

### 3.3 `03_execution.md`

必须记录实际执行事实，而非计划：

- 开始和结束时间；
- Git 分支及执行代码提交；
- Python和关键依赖版本；
- 实际命令；
- 行情清单、调整方式和数据哈希；
- 实际生成的候选数量；
- 测试、编译和依赖检查结果；
- 执行期间遇到的异常、原因和处理；
- 是否访问样本外数据。

执行文档不得将未运行的步骤写成已完成。

### 3.4 `04_conclusion.md`

必须记录：

- 最佳候选及冠军的同窗口收益率和夏普率；
- 每个验收窗口的 PASS/FAIL；
- 整体结论；
- 当前冠军是否改变；
- 研究假设是接受、拒绝还是证据不足；
- 是否产生冻结挑战者；
- 是否允许进入样本外测试；
- 下一轮建议，但不得把建议写成已经确认的研究结论。

### 3.5 `artifacts/`

必须保存能够复核结论的机器可读原始证据。本轮冠军—挑战者研究至少包括：

- `protocol.json`：预注册协议；
- `candidate_results.csv`：所有候选的完整逐窗口指标及排序字段；
- `champion_metrics.json`：冠军逐窗口指标；
- `metrics.json`：本轮汇总指标和可见数据哈希。

失败实验也必须保留全部候选，不得只保留最佳候选。以后新增证据文件可以放入 `artifacts/`，但不能替代四份必需文档。

### 3.6 `experiment_manifest.json`

清单至少包含：

- schema版本；
- 实验编号和日期；
- 实验状态；
- 证券代码和资产类型；
- 冠军版本和规则哈希；
- 可见样本截止日；
- 样本外是否被访问；
- 全部受管文件的相对路径、字节数和SHA-256。

清单不记录本机绝对路径或历史 `outputs/` R编号。清单自身不递归记录自身哈希。

## 4. 研究与回测的运行边界

### 4.1 研究入口

研究入口直接创建下一个 `experiments/MMDD_EXX/`，并将研究程序的机器结果写入其 `artifacts/`。研究执行完成后生成四份文档和清单。

研究目录不可覆盖。如果执行中断，目录保留并在执行文档和清单中标记 `INCOMPLETE` 或 `ERROR`；后续重跑必须创建新的实验编号，不复用失败目录。

### 4.2 普通回测入口

普通固定规则回测继续写入：

```text
outputs/<证券代码>_<MMDD>_RXX/
```

普通回测可以保存指标、订单、净值、因子、审计和HTML图表，但不得生成以下研究资料：

- 候选参数搜索结果；
- 候选排名；
- 研究协议；
- 挑战者冻结决定；
- 研究目标、设计或结论文档。

`outputs/` 继续保留在 `.gitignore` 中。

## 5. 本轮资料迁移

当前再入场实验迁移为：

```text
experiments/0824_EX01/
```

迁移来源与目标：

- `configs/experiments/588080_reentry_v1.json` → `artifacts/protocol.json`；
- 本机研究输出中的 `candidate_results.csv`、`champion_metrics.json`、`metrics.json` → `artifacts/`；
- `docs/experiments/588080_reentry_v1_result.json` 的内容转化并补充到四份文档和清单中；
- 原实施计划只作为开发历史保留在 `docs/superpowers/plans/`，不作为正式实验资料入口。

迁移后，旧的研究协议和结果文件从 `configs/experiments/`、`docs/experiments/` 删除，避免形成两个权威来源。研究型本机目录 `outputs/588080_0824_R02` 在逐文件哈希和内容校验通过后移除；普通回测目录 `outputs/588080_0824_R01` 保留。

## 6. 程序边界

新增独立的实验资料目录分配和完整性校验能力，不复用普通回测的 `create_output_dir`：

- `create_experiment_dir(root, run_date)`：按日期创建下一个 `MMDD_EXX`；
- `build_experiment_manifest(experiment_dir, metadata)`：枚举受管文件并计算哈希；
- `validate_experiment_archive(experiment_dir)`：检查必需文件、清单字段、文件大小和哈希。

研究编排器负责生成研究内容；目录工具只负责编号、清单和完整性，不理解具体策略指标。

`scripts/run_experiment.py` 改为写入 `experiments/`。`scripts/run_holdout.py` 只接受受Git跟踪实验目录中的冻结挑战者，且样本外结果应回写到新的一轮实验目录，而不是 `outputs/`。

## 7. 测试与验收

至少覆盖以下测试：

1. 空目录时创建当日 `EX01`。
2. 同日已有编号时创建最大编号加一。
3. 次日编号重置为 `EX01`。
4. 存在空号时不回填。
5. 已有目录不可覆盖，超过 `EX99` 明确失败。
6. 缺少任一四文档或必需机器证据时验证失败。
7. 文件被修改后哈希验证失败。
8. 研究入口不写入 `outputs/`。
9. 普通回测入口继续使用 `outputs/<代码>_<MMDD>_RXX/`。
10. 本轮迁移档案通过完整性验证，且候选数量和正式结论一致。

交付前执行项目的最小相关测试、完整快速离线测试、`compileall`、`pip check` 和 `git diff --check`。

## 8. 非目标

本次不做以下工作：

- 不改变冠军规则或本轮研究结论；
- 不重新执行候选搜索；
- 不访问2026样本外数据；
- 不合并或删除普通回测历史；
- 不把HTML图表、订单和净值等普通回测大文件纳入Git；
- 不建立数据库或远程实验跟踪服务。
