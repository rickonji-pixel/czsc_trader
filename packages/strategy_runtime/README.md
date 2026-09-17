# 策略运行时（Strategy Runtime，SRT）

SRT 是研究、确定性回测、模拟交易以及未来实盘交易共同使用的策略执行边界。
它负责策略特有的数据发布、决策计算、决策解释以及显式状态契约。

## 对象边界

- `StrategyFamily` 是长期存在的策略族身份，由 SM 治理和持久化。SRT 通过
  `RuntimeDefinition.strategy_family_id` 引用策略族，不重复保存策略族对象。
- 策略版本是 SM 发布的不可变版本。`RuntimeDefinition` 是该版本在 SRT 中的
  可执行投影，两者使用相同的 `release_id` 和 `release_hash` 标识身份。
- `DeploymentSpec` 将一个不可变策略版本绑定到交易标的、账户和执行渠道。
  部署所需的账户状态和策略状态分别通过 `AccountSnapshot` 和
  `StrategyStateSnapshot` 显式传入。
- `ExecutableStrategy` 通过 DFLS 发布其声明的数据输入，计算
  `StrategyDecision` 并解释该决策。数据发布状态未达到 `READY` 时，不允许进入
  决策计算。
- `ExecutionChannel` 通过支持幂等的宿主契约接收决策，执行渠道本身不包含策略逻辑。

SRT 不负责策略生命周期治理、策略评估、券商账户管理和进程守护。这些职责分别由
SM、SE、PTE/TDR 等执行宿主以及 WDG 承担。

## 统一执行流程

`StrategyRunner` 为研究、回测和交易宿主提供唯一的单周期编排流程：

1. 校验策略版本、部署和执行渠道的身份及能力是否兼容；
2. 调用策略通过 DFLS 发布数据，并校验每项输入的 `DataRequest` 与 `DataResult`
   是否配对，数据集、标的、频率和截止时间是否符合输入契约；
3. 数据未就绪时返回 `DATA_NOT_READY`，不读取账户、不计算决策、不调用渠道；
4. 使用显式账户快照和策略状态快照计算决策；
5. 校验决策的版本、时间、仓位边界、快照版本和输入数据身份；
6. 使用“渠道 ID＋决策 ID”作为幂等键提交执行请求；
7. 分别返回 `ACCEPTED` 或 `REJECTED`。渠道接受请求不代表订单已经成交，实际执行
   结果只能读取 `ExecutionReceipt.status`，避免形成“假成交”。

## 回测渠道

`BacktestChannel` 与未来的 Futu 模拟、实盘渠道遵守相同的 `ExecutionChannel`
协议。它负责能力声明、严格幂等和执行回执留存；具体成交规则由
`BacktestExecutionModel` 注入。这样可以复用现有回测成交模型，同时避免把撮合细节
或策略逻辑写入 SRT。

## 策略版本加载

`StrategyLoader` 接收完整的 SM 冻结版本记录，并在加载前重新计算和核对
`release_hash`。策略实现采用约定式定位：例如 `S002-v1` 对应
`strategy_runtime.strategies.s002_v1.S002V1`。因此新增策略版本只需要新增自己的
实现模块，无需修改中央分派表。

运行身份同时包含：

- SM 冻结版本的 `release_hash`；
- 策略实现源码的 `source_sha256`；
- 策略参数的 `parameters_sha256`。

三者共同形成 `runtime_sha256`，并写入每个决策。参数不变但实现代码发生变化时，
运行身份也会变化，历史决策仍可准确追溯到当时的代码和参数组合。

## 已迁移样板

`S002-v1` 是首个迁入 SRT 的冻结策略样板。它直接通过 DFLS 获取：

- 510500.SH 后复权日线，用于生成“三连跌”信号；
- 不复权日线，用于提供执行参考价；
- 上交所交易日历，用于确定下一有效交易日。

SRT 实现与旧 TDR 实现在相同数据截止日执行交叉回放，目标仓位保持一致。当前尚未
切换 TDR 或 PTE 的正式调用入口；旧链路继续运行，下一阶段再评审宿主迁移。
