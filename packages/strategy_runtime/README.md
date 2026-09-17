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

当前阶段只提供稳定的领域对象和协议，不包含具体策略、策略加载器和 Runner；这些
能力将在后续阶段评审通过后接入。
