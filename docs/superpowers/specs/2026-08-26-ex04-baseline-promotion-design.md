# 0824_EX04正式基线晋升设计

## 目标

将`experiments/0824_EX04/artifacts/frozen_challenger.json`逐字冻结为新的`baseline_20260826`，并设为当前活动基线。原`baseline_20260823`保留字节、哈希和历史验证快照，但标记为归档，只允许通过显式版本号复现历史，不再作为默认回测或后续研究比较对象。

新基线只适用于`588080.SH`。截至2026-08-24的数据已经被项目反复观察，不能因基线晋升重新成为留出样本；新基线的独立前向验证起点固定为2026-08-26。

## 已确认决策

- `baseline_20260826`成为注册表`latest`；
- `baseline_20260823`降为`archived`，不删除、不覆盖；
- 新基线是588080专属四层策略，不得用于其他标的；
- 588080普通回测未指定`--baseline`时加载`baseline_20260826`；
- 旧版本只有显式指定`--baseline baseline_20260823`时才能用于历史复现；
- 不创建新的2026表现验证快照，不重跑2026作为晋升依据；
- 不改写0824_EX04实验档案。

## 注册格式

`configs/rule_baselines/registry.json`升级为schema version 3。注册项增加：

- `status`：`active`或`archived`；
- `scope`：`symbol`或`historical_generic`；
- `symbol`：专属基线固定为`588080.SH`；
- `strategy`：旧版为`czsc_fixed_rule`，新版为`czsc_four_layer`；
- `source_path`和`source_sha256`：记录0824_EX04冻结文件身份；
- `selection_sample_end`：`2025-12-31`；
- `forward_validation_start`：`2026-08-26`。

`configs/rule_baselines/baseline_20260826.json`必须与0824_EX04冻结文件逐字节相同。注册表保存该文件的规范JSON哈希，同时保存源文件字节哈希；加载时同时验证配置文件规范哈希、源文件字节哈希以及两个文件的逐字节相等性。这样既保持现有注册表的可移植哈希口径，也证明晋升没有重拟合或手工重建参数。

旧注册项补充`status=archived`和`scope=historical_generic`，其原文件、SHA-256、验证快照和冻结时间保持不变。

## 解析模型

`ResolvedBaseline`扩展为可描述两种冻结策略：

1. `czsc_fixed_rule`：沿用三组聚合权重、阈值和状态机；
2. `czsc_four_layer`：包含固定因子名称、按因子权重、入场/离场阈值、来源身份和标的范围。

解析四层基线时，根据冻结文件内的`champion.version`显式加载归档的`baseline_20260823`，只继承其确认日、最短持仓、退出确认日和入场门控状态机；四层策略的分数权重及入退场阈值完全来自0824_EX04冻结文件。冻结文件中的冠军哈希必须与归档注册项一致，防止状态机来源漂移。

解析器接受可选`symbol`。活动基线具有标的范围时，传入不同标的必须失败。未指定版本时选择`latest`；显式指定归档版本允许历史复现，并在解析结果与普通回测manifest中标记`status=archived`。

## 统一执行

新增一个小型基线执行适配层，输入完整因子帧和`ResolvedBaseline`，统一返回现有`AppliedRule`结构：目标仓位、因子分数和因子事件。

- 固定规则基线继续调用`apply_fixed_rule`；
- 四层基线从生成的raw信号中选择冻结的12个因子，执行现有`normalized_signal_factors`、`validate_fixed_factor_weights`、`score_four_layer`和`positions_from_scores`；
- 四层事件使用冻结分数、阈值和仓位变化生成稳定`factor_event_id`，供现有次日开盘与无前视审计复用；
- 缺少、重复或乱序的冻结因子立即失败，不允许用零填充缺失因子；
- 新执行路径不生成候选、不优化参数、不访问研究档案以外的策略定义。

`run_fixed_backtest`在创建输出目录后，以请求标的解析基线并调用统一适配层。输出仍保留`baseline_rule.json`以兼容已有消费方，但对四层策略保存的是完整冻结基线payload；manifest新增`strategy`、`status`、`scope`、`source_path`、`source_sha256`、`selection_sample_end`和`forward_validation_start`。

## 默认与错误行为

- `588080.SH`未指定版本：执行`baseline_20260826`；
- `588080.SH`显式指定`baseline_20260823`：允许，仅用于历史复现，manifest标记归档；
- 其他标的未指定版本：因当前活动基线是588080专属而明确失败，不静默回退旧基线；
- 其他标的显式指定`baseline_20260826`：明确失败；
- 其他标的显式指定`baseline_20260823`：技术上允许历史复现，但报告和manifest明确标记归档，不代表当前推荐策略；
- 修改新基线文件、源EX04文件、来源哈希或旧冠军哈希：加载立即失败。

## 文档与研究口径

更新`docs/RESEARCH_HANDOFF.md`：

- 当前活动正式基线为`baseline_20260826`，来源是0824_EX04；
- `baseline_20260823`仅作历史归档；
- 0826_EX01结论为`no_stable_boundary`，支持停止历史回溯修补；
- 2026全部已观察，不再作为后续实验的独立留出；
- 新证据只能来自2026-08-26之后的前向行情或另行批准的独立信息。

历史实验文档和manifest不改写；它们继续引用当时实际使用的`baseline_20260823`，保证研究事实不被事后重命名。

## 测试与验收

先写失败测试，再实现：

1. 注册表默认解析为`baseline_20260826`且来源字节与0824_EX04完全一致；
2. 新基线正确解析12个因子、L1权重、0.175入场阈值、0.025离场阈值及旧状态机；
3. 新基线拒绝其他标的，归档基线只有显式指定才能加载；
4. 四层适配器在合成因子帧上产生预期分数、仓位和事件；
5. 588080默认普通回测manifest记录活动四层基线及完整来源；
6. 新执行路径的目标仓位与0824_EX04冻结策略在受跟踪数据上完全一致；
7. 现有显式旧基线回测和历史研究解析不回归；
8. 聚焦测试、档案校验、Python编译和`git diff --check`通过。

本次只晋升和接通冻结基线，不运行新的研究或2026验收，不创建`outputs/`研究证据。
