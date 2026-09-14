# CZSC Trader

## 项目介绍

CZSC Trader 是面向个人量化团队的可审计策略研发与模拟交易项目。它把行情准备、策略研究、
候选评估、版本冻结、交易决策、模拟执行和前瞻监测连接成一条可复核的工作流。

项目强调证据边界和执行一致性：研究结论必须能回到不可变实验档案，正式策略必须绑定明确
版本和数据截止，交易决策与渠道执行通过稳定契约衔接，运行异常必须留下可追踪的审计记录。

本 README 只承担项目介绍、项目目录说明和关键文档索引。安装命令、当前策略状态、运行操作
与研究进度分别由对应文档维护，避免项目首页演变成易过期的操作手册或状态快照。

### 系统组成

| 模块 | 简称 | 位置 | 职责 |
| --- | --- | --- | --- |
| CZSC Trader | TDR | `src/czsc_trader/` | 行情验证、策略解析、回测、研究编排和交易决策 |
| Dataflows | — | `packages/dataflows/` | Tushare数据获取、复权、多频数据处理和发布清单 |
| Factor & Signal Catalog | FSC | `packages/factor_signal_catalog/` | 项目级信息族、因子和信号定义目录 |
| Strategy Template Catalog | STC | `packages/strategy_template_catalog/` | 策略函数模板、输入角色和参数边界目录 |
| Strategy Manager | SM | `packages/strategy_manager/` | 策略身份、版本、资格、冻结和证据治理 |
| Strategy Evaluator | SE | `packages/strategy_evaluator/` | 候选比较、统计审计、稳健性检验和晋级建议 |
| Paper Trading Engine | PTE | `packages/paper_trading_engine/` | 虚拟账户、模拟下单、成交对账、运行审计和控制台 |
| PTE Watchdog | WDG | PTE包内 | PTE进程托管、健康检查和故障拉起 |

TDR通过`advice.v4/advice.v5` JSON契约向PTE提供普通调仓决策或带执行时点、成交依赖的计划。
PTE负责执行与账户状态，不参与策略计算、定价或改量；Futu渠道只负责订单、成交和持仓回报。
SM与SE管理策略生命周期和评估证据，不直接参与运行时下单。

### 核心工作流

```text
受控数据 → 策略研究 → 不可变实验 → 候选评估 → 人工冻结
        → TDR交易决策 → PTE模拟执行 → Futu对账 → 前瞻监测
```

研究诊断、正式裁决和模拟盘表现分别保存，不能用单次成交或未冻结实验替代策略有效性证据。
历史失败、执行异常和修复记录继续保留，避免事后改写研究或交易结果。

## 项目目录

| 路径 | 内容 | 管理边界 |
| --- | --- | --- |
| `src/czsc_trader/` | TDR主程序、命令入口和应用服务 | 项目核心业务代码 |
| `packages/` | Dataflows、SM、SE、PTE等独立子包 | 各子系统接口、实现和包级测试 |
| `catalog/` | FSC信息族、因子和信号定义 | 项目级定义来源，不保存标的值或Alpha证据 |
| `strategy_templates/` | STC策略函数模板定义 | 项目级结构来源，不保存搜索结果或绩效证据 |
| `strategies/` | 正式策略身份、冻结版本、生命周期和证据 | 正式策略事实来源，不保存研究草稿 |
| `research/` | 各策略研究线的交接、候选和监测方案 | 当前研究上下文与下一步入口 |
| `experiments/` | 按策略和实验编号归档的输入、结果及审计证据 | 不可变研究档案，失败实验同样保留 |
| `data/` | 受控研究数据、执行价格及数据清单 | 研究输入；具体口径由清单和交接文档定义 |
| `docs/` | 用户、研究、开发、治理、事故和历史设计文档 | 跨模块说明与关键文档入口 |
| `tests/` | TDR端到端功能测试和共享测试支持 | 根项目的行为契约验证 |
| `state/` | PTE数据库、发布数据、图表、日志等本机运行状态 | Git忽略；不能当作可移植研究证据 |
| `outputs/` | 本地回测报告、图表和临时导出结果 | Git忽略；正式证据应归档到`experiments/` |
| `pyproject.toml` | 根项目依赖、命令入口、测试与静态检查配置 | Python项目配置来源 |

`.venv/`、`.tmp/`、`__pycache__/`及各包的`build/`、`dist/`属于本机环境或生成物，
不构成项目文档和正式证据。

## 关键文档索引

| 使用场景 | 首要入口 | 主要内容 |
| --- | --- | --- |
| 安装、更新数据、运行回测或操作PTE | [用户使用说明](docs/USER_GUIDE.md) | 安装命令、数据流程、策略命令、控制台、服务启停和故障处理 |
| 查看全部策略的当前阶段和下一步 | [研究项目索引](research/README.md) | S001—S005研究状态、交接入口和前瞻监测方案 |
| 创建实验、评估候选或冻结策略 | [研究交接](docs/RESEARCH_HANDOFF.md) | 研究工作流、数据污染边界、候选评估和冻结规则 |
| 核对团队统一的收益、风险和证据目标 | [OPC交易策略研究目标](research/RESEARCH_MANDATE.md) | 策略研究的通用目标与约束 |
| 查阅或归档正式实验 | [实验档案说明](experiments/README.md) | 实验命名、目录结构、产物契约和不可变规则 |
| 理解架构、修改代码或恢复开发环境 | [技术交接](docs/DEVELOPMENT_HANDOFF.md) | 架构契约、代码入口、环境恢复和验证方式 |
| 查看已确认运行事故 | [事故复盘索引](docs/incidents/README.md) | 事故时间线、影响、修复证据和防复发措施 |
| 维护测试体系 | [测试用例治理](docs/TEST_GOVERNANCE.md) | 测试分层、边界覆盖和周期性治理 |

### 包级技术文档

- [Dataflows技术说明](packages/dataflows/README.md)：数据包接口和使用方式。
- [FSC技术说明](packages/factor_signal_catalog/README.md)：因子与信号定义目录及查询方式。
- [STC技术说明](packages/strategy_template_catalog/README.md)：策略函数模板、实例化契约及边界。
- [Strategy Evaluator技术说明](packages/strategy_evaluator/README.md)：候选评估与统计审计接口。
- [Paper Trading Engine技术说明](packages/paper_trading_engine/README.md)：模拟交易引擎的配置、
  接口、运行边界和包级开发信息。

### 历史设计资料

- [`docs/superpowers/specs/`](docs/superpowers/specs/)：已批准设计说明。
- [`docs/superpowers/plans/`](docs/superpowers/plans/)：对应实施计划和历史执行路径。

历史设计资料用于审计设计演进，不代表当前操作入口。开始使用项目时先阅读用户使用说明；
继续策略研究时从研究项目索引进入；修改代码或运行测试时以技术交接为准。
