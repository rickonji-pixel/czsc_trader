# Trader 功能测试收敛设计

**日期：** 2026-09-04  
**状态：** 已批准  
**分支：** `codex/functional-test-consolidation`

## 1. 背景

Trader 当前 `tests/` 下有 24 个测试文件、106 个测试函数；参数化展开后共收集 109 项，其中默认运行 106 项、研究档案标记 3 项。现有测试大量保留了 TDD 过程中的算法细节、历史实验修正和仓库当前状态断言，造成以下问题：

- 同一用户能力被多个细粒度测试重复覆盖；
- 测试直接读取持续演进的正式策略注册表，冻结新版本后发生无功能故障的失败；
- 历史实验重算进入测试路径，两个全规模候选等价测试约耗时 31 秒；
- 测试数量随开发轮次持续增长，OPC 团队的维护成本不可控。

本设计将 Trader 测试从“实现细节集合”收敛为“用户可调用能力的完整验证”。

## 2. 目标与原则

Trader 只保留 8 个功能场景。每个场景从公开 CLI 或应用服务入口开始，验证完整业务结果和关键安全契约。

原则如下：

1. 一个测试对应一项完整用户能力，不按内部函数拆分测试。
2. 同一规则只在最接近用户结果的场景中验证一次。
3. 使用临时仓库、固定小数据和本地模拟适配器，测试结果不依赖正式注册表、实时行情或运行中的 PTE。
4. 历史实验事实由不可变实验档案保存，功能测试不重新执行历史搜索。
5. TDD 阶段可临时增加聚焦用例；功能场景覆盖完成后删除临时用例。
6. 安全规则必须通过可观察的订单、成交、净值、审计或错误结果得到验证。

## 3. 保留的八个功能场景

### FT-T01 行情准备与验证

执行 `data prepare` 和 `data validate`，使用模拟 Tushare 响应完成行情发布与验证。

覆盖：

- ETF 日线、分钟线与交易日历适配；
- 后复权研究行情和未复权执行价格分别发布；
- 显式运行时数据目录；
- 数据身份校验；
- 文件篡改后验证失败。

### FT-T02 Advice 完整交易周期

使用固定行情和账户夹具，依次运行空仓入场、未成交重试、持仓和离场决策。

覆盖：

- 冻结策略版本解析和 `advice.v4` 契约；
- 下一交易日执行；
- ETF 最小价格变动和 100 股整数手；
- 费率覆盖后的可用资金；
- 限价触及和不利滑点；
- 目标仓位与实际仓位分离；
- 同一入场周期目标数量稳定；
- 非法账户数量被拒绝。

### FT-T03 固定策略回测

通过 CLI 运行一次小型完整回测。

覆盖：

- 冻结策略加载；
- 信号日与次日开盘执行；
- 订单、成交、净值和交易成本；
- 最大回撤、卡玛、盈亏比、收益率和无闭合交易状态；
- manifest、audit、CSV、Markdown 报告和 HTML 图表；
- 执行订单价格和手数合法。

### FT-T04 图表交互回归

生成包含主图和副图的离线 HTML，验证：

- 主副图共享横轴；
- 每个交易日均有主图悬停目标；
- 非交易日折叠；
- 买卖信号视觉标记存在；
- HTML 可独立打开。

该场景独立保留，用于防止已发生过的 K 线瞄准故障回归。

### FT-T05 策略管理 CLI 生命周期

在临时注册表中执行：

`create → version → freeze → list/show/validate → history → evidence → performance → promote → downgrade → retire`

同时验证 `baseline list/show/validate` 动态指向正式策略身份。测试不写死当前活动版本或事件数量。

### FT-T06 候选评估与接受

使用小型确定性候选池执行：

`strategy evaluate → 筛劣 → 多窗口排名 → 冠军审计 → 证据生成 → strategy accept-evaluation → SM 冻结`

覆盖：

- 每个候选均进入筛选审计台账；
- OPC 排名与统计稳健性审计；
- 缓存精确命中及篡改失效；
- 完整结果原子写入；
- 只有允许冻结的结论可以接受；
- 接受重试不会重复冻结。

### FT-T07 实验档案验证

创建临时实验档案，验证初次通过、文件修改后失败。

覆盖：

- JSON 与原始字节身份；
- 跨平台换行兼容；
- manifest 完整性；
- 文件遗漏和篡改拒绝；
- 运行时缓存不进入档案；
- 验收日志允许追加。

正式仓库全部历史档案的验证仍由 `archive validate` 操作命令承担，不进入日常功能回归。

### FT-T08 安装入口与命令面

只启动一次安装后的 `czsc-trader` 可执行程序，验证六个一级入口及其必要子命令：

- `data`；
- `baseline`；
- `strategy`；
- `backtest`；
- `advice`；
- `archive`。

同时验证 JSON 输出、退出码和标准错误通道。

## 4. 现有测试迁移与删除映射

| 现有文件 | 测试数 | 处理 |
|---|---:|---|
| `test_backtest_core.py` | 1 | 合并到 FT-T03，删除原文件 |
| `test_backtest_report.py` | 1 | 合并到 FT-T03，删除原文件 |
| `test_candidate_evaluation.py` | 11 | 必要结果合并到 FT-T06；删除原文件及两个全规模等价测试 |
| `test_charting.py` | 1 | 改写为 FT-T04 |
| `test_cli_e2e.py` | 2 | 回测能力并入 FT-T03；安装入口并入 FT-T08 |
| `test_cli_imports.py` | 1 | 删除；延迟导入属于性能实现细节 |
| `test_evaluation_artifacts.py` | 3 | 缓存命中和篡改失效并入 FT-T06 |
| `test_evaluation_audit.py` | 1 | 并入 FT-T06 |
| `test_evaluation_service.py` | 13 | 成功评估、接受与幂等语义并入 FT-T06；参数排列测试删除 |
| `test_execution_policy.py` | 13 | 合并到 FT-T02 |
| `test_execution_prices.py` | 3 | 合并到 FT-T01 |
| `test_identity_and_archives.py` | 8 | 临时档案语义并入 FT-T07；全仓历史验证退出日常回归 |
| `test_opc_v2_revaluation.py` | 3 | 删除；属于历史实验过程 |
| `test_opc_v3_statistical_audit.py` | 1 | 删除；属于历史实验绑定测试 |
| `test_range_optimization.py` | 3 | 当前必要结果并入 FT-T06，删除原文件 |
| `test_range_optimization_correction.py` | 1 | 删除；属于历史修正过程 |
| `test_range_optimization_v2.py` | 2 | 删除；属于历史候选构造过程 |
| `test_range_weight_platform.py` | 7 | 当前必要结果并入 FT-T06，删除原文件 |
| `test_range_weight_research.py` | 4 | 删除；属于历史研究过程 |
| `test_repository_contract.py` | 9 | 用户命令和动态身份并入对应功能场景；硬编码及依赖结构断言删除 |
| `test_robustness.py` | 4 | 删除；冠军统计审计能力归属 SE |
| `test_strategy_cli.py` | 8 | 合并为 FT-T05 和 FT-T06 |
| `test_strategy_metrics.py` | 4 | 可观察指标结果并入 FT-T03 |
| `test_strategy_registry.py` | 2 | 删除；SM 功能测试负责注册表领域能力 |

## 5. 测试组织

Trader 功能测试放入：

```text
tests/functional/
├── conftest.py
├── test_archive.py
├── test_backtest.py
├── test_charting.py
├── test_cli_surface.py
├── test_data_and_advice.py
├── test_evaluation.py
└── test_strategy_lifecycle.py
```

测试名称使用 `test_ft_tNN_*` 前缀，使失败结果能够直接对应本设计中的功能场景。

共享夹具只负责构建固定输入和模拟外部适配器，不包含业务断言。

## 6. 外部系统边界

日常功能回归不执行以下操作：

- 访问实时 Tushare；
- 连接 Futu OpenD；
- 读取或修改正式策略注册表；
- 读取运行中的 PTE 数据库；
- 安装、停止或修改 Windows 服务；
- 重跑正式研究候选全集。

外部服务可用性继续通过独立的运行检查或人工验收确认。

## 7. 删除顺序

为防止覆盖缺口，按以下顺序执行：

1. 建立共享临时仓库和固定数据夹具；
2. 新增 8 个功能场景；
3. 每个功能场景通过后，删除其映射的旧测试；
4. 运行 Trader 功能回归；
5. 检查测试目录中没有遗留历史实验测试；
6. 更新 README 和开发交接文档中的测试命令。

## 8. 验收标准

1. Trader 默认测试只收集 FT-T01 至 FT-T08，共 8 个功能场景。
2. 六个公开 CLI 命令族均有完整功能覆盖。
3. Advice 的价格、手数、资金、交易日和仓位安全规则均通过最终订单结果验证。
4. 评估、冠军审计、接受和冻结形成一条完整链路。
5. 测试不依赖正式活动策略版本，冻结新版本不会导致测试失效。
6. 测试不访问实时外部服务。
7. 同一设备冷启动运行不超过 30 秒，目标值为 15 至 20 秒。
8. 旧的 24 个细粒度测试文件完成迁移或删除。
9. 正式策略配置、历史实验档案和生产代码行为不因测试收敛发生变化。
10. `git diff --check` 通过。

## 9. 非目标

- 本轮不修改 Trader 产品功能；
- 不重新计算或改变历史研究结论；
- 不调整 S001 策略、活动版本或执行规则；
- 不修改 SM、SE、PTE 的测试集；
- 不以并行测试掩盖重复用例；
- 不把实时外部系统可用性纳入确定性回归。
