# CZSC Trader 统一入口架构设计

## 背景

仓库已经形成数据准备、冻结基线回测、研究实验、历史复现和档案校验等完整能力，但入口和编排职责逐步分散：

- `scripts/` 下存在多个彼此独立的用户入口；
- 部分 `*_runner.py` 同时包含领域逻辑、文件编排和命令行参数；
- `scripts/run_experiment.py` 通过大型条件分派绑定多代实验；
- 普通回测、正式研究、历史复现和档案验证缺少统一请求、结果与错误契约；
- 新会话需要阅读内部实现才能判断应使用哪个入口。

本轮重构在不改变冻结策略、研究结论、交易语义和实验档案的前提下，建立唯一、稳定、可自动化的功能入口，并删除迁移后废弃的代码与测试。

## 目标

1. 以 `czsc-trader` 作为唯一命令行入口。
2. 明确 CLI、应用编排、核心领域、研究和报告的依赖方向。
3. 使用显式实验处理器注册表替代集中式实验编号条件分派。
4. 统一请求、结果、JSON 输出、错误代码和输出目录语义。
5. 从机制上保护冻结研究档案，禁止原地重跑或覆盖。
6. 保留所有仍可用于验证或复现 Git 研究档案的能力。
7. 删除旧脚本、模块内 CLI、重复编排、无调用代码和对应失效测试。
8. 保证当前 `baseline_20260826` 回测结果以及全部研究档案在迁移前后保持一致。

## 非目标

- 不修改 0824EX04 的因子、权重、阈值或状态机。
- 不启动新研究，不搜索参数，不重新解释既有 PASS/FAIL 结论。
- 不改变行情来源、后复权方式、费用、次日开盘执行或因果审计规则。
- 不引入动态插件发现、第三方扩展协议或远程执行平台。
- 不保留旧命令兼容包装器、旧模块别名或双入口过渡期。
- 不把实盘账户或成交状态纳入本轮架构。

## 总体架构

包结构按职责组织：

```text
src/czsc_trader/
├── cli/             # 唯一命令行入口；参数、输出与退出码
├── application/     # 数据、基线、回测、实验和档案用例编排
├── core/            # 行情、因子、策略、回测与审计
├── research/        # 实验协议、处理器注册表与研究实现
└── reporting/       # 指标、图表、报告和输出发布
```

依赖方向固定为：

```text
CLI -> Application -> Core / Research / Reporting
Research -> Core / Reporting
```

`Core` 不得依赖 `Research`、`Application` 或 `CLI`。`Research` 不得依赖 `CLI`。命令行层不得直接读写研究产物或实现交易逻辑。

为控制迁移风险，现有稳定领域模块可以先保留文件位置，由新分层模块通过明确接口调用；只有在职责确实重复或文件移动能消除反向依赖时才移动。最终架构约束以依赖和公共接口为准，不以追求目录移动数量为目标。

## 唯一命令体系

`pyproject.toml` 注册唯一 console script：

```text
czsc-trader = czsc_trader.cli.main:main
```

首批命令：

```text
czsc-trader data prepare
czsc-trader data validate
czsc-trader baseline list
czsc-trader baseline show
czsc-trader baseline validate
czsc-trader backtest run
czsc-trader experiment run
czsc-trader experiment replay
czsc-trader archive validate
```

命令约束：

- `backtest run` 只执行已注册、哈希正确且适用于目标证券的冻结基线。
- `experiment run --dir <目录>` 只允许运行尚未冻结的新实验目录。
- `experiment replay --dir <冻结目录> --output <目录>` 只能写入源实验目录之外的隔离输出。
- `archive validate <目录>` 验证单个研究档案；`archive validate --all` 验证全部 Git 跟踪实验档案。
- 默认从当前目录向上发现仓库根目录，也允许 `--repo-root` 显式指定。
- 所有帮助、参数和结果均以证券全代码、实际目录和明确基线版本为准，不假定设备绝对路径。

旧 `scripts/*.py` 入口以及 runner 模块中的 `argparse`、`main()` 和 `__main__` 分支在迁移后删除，不提供兼容转发。

## 应用层契约

每个用例由不可变请求类型和结果类型组成。CLI 只负责把参数转换为请求，并把结果转换为输出信封。

公共上下文 `RepositoryContext` 负责：

- 发现并验证仓库根目录；
- 提供 `data/raw`、`configs`、`experiments` 和 `outputs` 的规范路径；
- 拒绝逃逸仓库或命令明确输出根目录的路径；
- 不依赖历史 outputs、虚拟环境或 SQLite 状态。

统一执行顺序：

```text
CLI 参数
  -> 强类型 Request
  -> RepositoryContext
  -> Application Service 前置校验
  -> Core 或 Research Handler
  -> 因果与完整性审计
  -> 临时目录完整落盘
  -> 原子发布
  -> 强类型 Result
  -> JSON 输出
```

应用服务按用例拆分，不建立全能 Service 类：

- `DataService`
- `BaselineService`
- `BacktestService`
- `ExperimentService`
- `ArchiveService`

服务依赖通过构造参数或函数参数显式传入，使文件系统和计算组件可以在测试中替换。

## 实验处理器注册表

研究层定义统一 `ExperimentHandler` 合同：

- `handler_id`
- `validate_protocol(context, experiment_dir)`
- `run(context, experiment_dir)`
- `replay(context, experiment_dir, output_dir)`

处理器负责实验特定协议和算法；Application 负责冻结保护、路径检查、事务发布、错误转换和统一结果。

新实验的协议显式声明 `handler`。冻结历史档案不得为适配新架构而改写；显式静态映射为历史实验编号选择处理器。映射属于可审计迁移元数据，不使用目录内容猜测、导入扫描或动态插件发现。

注册时检查：

- `handler_id` 唯一；
- 历史实验映射无重复、无未知处理器；
- 新协议引用已注册处理器；
- handler 的能力与命令动作相符。

## 输出契约

默认 `stdout` 只输出一份 UTF-8 JSON：

```json
{
  "status": "PASS",
  "command": "backtest.run",
  "result": {},
  "artifacts": {
    "output_dir": "命令实际返回的目录"
  },
  "warnings": []
}
```

进度和诊断日志只写 `stderr`。`--format text` 可输出面向人的精简文本，但不改变结果语义。JSON 字段和错误代码属于稳定外部契约。

成功退出码为 `0`。错误退出码：

- `2`：参数或协议错误；
- `3`：数据、基线或档案校验失败；
- `4`：冻结边界或路径安全违规；
- `5`：执行或审计失败；
- `10`：未预期内部错误。

失败 JSON 包含稳定的 `error.code`、简明消息和必要上下文。完整堆栈仅在 `--debug` 下写入 `stderr`。

## 冻结与路径安全

- 有效冻结清单存在时，`experiment run` 必须在加载研究数据前拒绝。
- `experiment replay` 输出不得等于源目录、位于源目录内或覆盖已有目录。
- replay 前后必须验证源实验受跟踪文件字节不变。
- 回测必须验证基线注册状态、策略哈希、来源哈希、标的范围和所需频率。
- 研究处理器必须声明数据截止日期；应用层在加载数据前执行边界校验。
- 普通回测、正式研究和历史复现使用不同输出类别和清单类型。
- 所有产物先写同一文件系统上的临时目录，通过审计后原子改名发布。
- 默认拒绝覆盖；普通回测继续使用自动递增运行编号。
- 失败时清理本轮临时目录，不修改已存在产物。

## 清理策略

清理以可证明的替代关系为前提：

1. 新 CLI 契约测试先建立并失败。
2. Application 服务接入现有稳定领域能力。
3. 每迁移一个入口，先完成等价回归，再删除对应旧入口。
4. 实验分派迁入显式注册表后删除 `run_experiment.py` 的条件分派。
5. 删除 runner 内 CLI 后，删除只覆盖旧参数解析和旧调用路径的测试。
6. 使用导入搜索、测试收集和全套回归确认无调用代码，再删除重复函数、类型和常量。
7. 不创建 deprecated 包装器、旧模块别名或兼容脚本。

已有有效领域测试迁移导入路径后保留。测试不能因为实现移动而降低交易、因果、哈希或档案断言强度。

## 测试设计

### CLI 契约

- 唯一 console script 可执行；
- 命令树、必填参数和帮助稳定；
- 成功与失败退出码正确；
- 默认 stdout 是单一 JSON；
- stderr 日志不污染 JSON；
- `--format text` 不改变底层结果。

### 应用层

- 请求校验和 RepositoryContext 路径发现；
- 服务编排顺序；
- 错误类型到退出码和 JSON 的映射；
- 临时目录发布与失败清理；
- 默认拒绝覆盖。

### 研究处理器

- 注册表唯一性和完整历史映射；
- 新协议 handler 解析；
- 冻结目录拒绝 run；
- replay 输出隔离；
- replay 前后源档案字节不变。

### 架构约束

通过静态导入扫描测试禁止：

- Core 导入 Research、Application 或 CLI；
- Research 导入 CLI；
- CLI 直接导入实验具体 runner；
- 生产 runner 再次出现 `argparse` 或 `__main__`。

### 等价与完整性

- `baseline_20260826` 在 588080 既定窗口的指标、订单和仓位与迁移前一致；
- 其他证券不能静默使用 588080 专属基线；
- 全部 Git 跟踪实验档案验证通过；
- 冻结实验文件哈希不变；
- 因果审计保持 PASS。

## 交付验收

交付前必须执行并报告：

1. 完整 pytest；
2. `compileall`；
3. 全部实验档案验证；
4. 588080 当前基线等价回归；
5. CLI 端到端 smoke；
6. 架构约束测试；
7. `git diff --check`；
8. `git status --short --branch`。

文档最终只展示唯一 `czsc-trader` 命令体系。普通回测结果仍只进入 `outputs/`，研究事实仍只来自 Git 跟踪实验档案。
