# SM、SE、PTE 功能测试收敛设计

**日期：** 2026-09-04  
**状态：** 已授权执行  
**分支：** `codex/functional-test-consolidation`

## 1. 统一原则

延续 Trader 已确认的测试治理方式：默认测试只验证用户或上层模块可观察的完整能力；同一规则只在最终业务结果处验证一次；临时 TDD 排列组合、私有函数细节和重复跨层断言删除。

所有场景使用固定输入、临时目录和模拟外部适配器，不连接实时行情、Futu OpenD，不修改 Windows 服务和本机 PTE 状态。

## 2. Strategy Manager

现状为 3 个文件、13 个测试函数、参数化后 16 项。收敛为 2 个场景：

1. `FT-SM01` 完整生命周期：创建策略、创建版本、冻结、记录研究/模拟盘证据、晋升、降级、退役、重新打开注册表并核对事件与部署资格。
2. `FT-SM02` 治理保护：模型严格校验、名称唯一、版本连续、证据要求、失败操作原子性、冻结版本篡改检测、退役不可恢复。

原 `test_models.py`、`test_registry.py`、`test_lifecycle.py` 由以上两个场景替换后删除。

## 3. Strategy Evaluator

现状为 12 个文件、49 个测试函数。收敛为 3 个场景：

1. `FT-SE01` 候选漏斗：协议验证、劣质筛选、相对非劣、Pareto 分层、冠军选择、目标达成及最终决策。
2. `FT-SE02` 临时冠军完整审计：PBO/DSR、配对区块 Bootstrap、参数邻域、执行因果性、试验台账、复现性和成本/滑点压力，输出 `PASS` 与 `MIXED` 风险标签。
3. `FT-SE03` 治理与报告：冻结值对象、统一默认值只能收紧、无效协议拒绝、证据不足结论、结果序列化和中文摘要渲染。

原 12 个测试文件全部删除。统计公式不再按每个私有算子分别建测试；固定矩阵经完整审计结果验证。

## 4. Paper Trading Engine

现状为 16 个 Python 文件、93 个测试函数（参数化后 105 项）以及 11 个 JavaScript 测试。收敛为 7 个 Python 场景和 1 个 JavaScript 场景：

1. `FT-PTE01` 多虚拟账户：创建、独立资金/持仓/账本、暂停恢复、重启持久化、旧账户身份迁移。
2. `FT-PTE02` 交易闭环：获取 advice、生成稳定意图、提交、部分成交递增对账、完整成交、退出、重启幂等和拆单。
3. `FT-PTE03` 渠道安全：显式策略绑定、Futu `SIMULATE` 锁定、代码/手数/价格校验、订单与虚拟账户审计归属、历史无证据不猜测。
4. `FT-PTE04` 调度发布：19:00 后发布、退避重试、交易窗口、隔夜决策执行、发布失败与恢复事件。
5. `FT-PTE05` 控制台：多账户资源路由、账户详情、决策/订单/成交、Futu 归属列表、暂停/恢复、二次确认撤单、系统事件和和平重启。
6. `FT-PTE06` Watchdog 与 Windows 服务：端口探测、进程健康、连续失败重启、退避、自动启动和 crash recovery 命令，不实际操作服务。
7. `FT-PTE07` 绩效证据：从虚拟账本导出与策略版本绑定的自包含绩效证据，核对收益、回撤、交易和哈希。
8. `FT-PTEJS01` 页面状态机：把现有 11 个 JavaScript 小测试合并为一次完整的多账户导航、竞态保护、告警、归属和北京时间渲染场景。

原 16 个 Python 测试文件和原 JavaScript 多用例文件在替代场景通过后删除。

## 5. 目标结构

```text
packages/strategy_manager/tests/functional/test_strategy_manager.py
packages/strategy_evaluator/tests/functional/test_strategy_evaluator.py
packages/paper_trading_engine/tests/functional/
├── conftest.py
├── test_accounts.py
├── test_engine.py
├── test_channel.py
├── test_scheduler.py
├── test_web.py
├── test_watchdog.py
├── test_performance.py
└── console_state.test.mjs
```

各包 `pyproject.toml` 的 `testpaths` 指向自身 `tests/functional`。

## 6. 验收标准

1. SM 默认只收集 2 个场景；SE 默认只收集 3 个场景；PTE 默认只收集 7 个 Python 场景和 1 个 JavaScript 场景。
2. Trader 仍只收集 FT-T01 至 FT-T08。
3. 四个 Python 模块联合运行全部通过，且无 `conftest` 名称冲突。
4. PTE JavaScript 场景通过。
5. 56 个研究档案继续通过产品命令验证。
6. 测试不访问实时外部系统，不修改正式配置、实验和状态目录。
7. 四模块 Python 功能回归冷启动目标不超过 30 秒。
8. Ruff、compileall、pip check 和 `git diff --check` 通过。

## 7. 非目标

- 不修改产品功能和模块边界；
- 不改变交易安全规则；
- 不调整策略、评估标准或 PTE 运行参数；
- 不用并行执行掩盖重复用例；
- 不保留仅证明私有实现方式的测试。
