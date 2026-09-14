# CZSC Trader

CZSC Trader 是面向个人量化团队的可审计策略研发与模拟交易项目。它把行情准备、策略研究、
候选评估、版本冻结、交易决策、模拟执行和前瞻监测连接成一条可复核的工作流。

项目强调证据边界和执行一致性：研究结论必须能回到不可变实验档案，正式策略必须绑定明确
版本和数据截止，交易决策与渠道执行通过稳定契约衔接，运行异常必须留下可追踪的审计记录。

本 README 只承担项目介绍和文档索引。安装命令、当前策略状态、运行操作与研究进度分别由
对应文档维护，避免项目首页演变成易过期的操作手册或状态快照。

## 项目组成

| 模块 | 简称 | 位置 | 职责 |
| --- | --- | --- | --- |
| CZSC Trader | TDR | `src/czsc_trader/` | 行情验证、策略解析、回测、研究编排和交易决策 |
| Dataflows | — | `packages/dataflows/` | Tushare数据获取、复权、多频数据处理和发布清单 |
| Strategy Manager | SM | `packages/strategy_manager/` | 策略身份、版本、资格、冻结和证据治理 |
| Strategy Evaluator | SE | `packages/strategy_evaluator/` | 候选比较、统计审计、稳健性检验和晋级建议 |
| Paper Trading Engine | PTE | `packages/paper_trading_engine/` | 虚拟账户、模拟下单、成交对账、运行审计和控制台 |
| PTE Watchdog | WDG | PTE包内 | PTE进程托管、健康检查和故障拉起 |

TDR通过`advice.v4/advice.v5` JSON契约向PTE提供普通调仓决策或带执行时点、成交依赖的计划。
PTE负责执行与账户状态，不参与策略计算、定价或改量；Futu渠道只负责订单、成交和持仓回报。
SM与SE管理策略生命周期和评估证据，不直接参与运行时下单。

## 核心工作流

```text
受控数据 → 策略研究 → 不可变实验 → 候选评估 → 人工冻结
        → TDR交易决策 → PTE模拟执行 → Futu对账 → 前瞻监测
```

研究诊断、正式裁决和模拟盘表现分别保存，不能用单次成交或未冻结实验替代策略有效性证据。
历史失败、执行异常和修复记录继续保留，避免事后改写研究或交易结果。

## 文档索引

### 使用与运行

- [用户使用说明](docs/USER_GUIDE.md)：安装、数据更新、回测、策略查看、PTE控制台、服务启停
  和常见故障处理。
- [PTE技术说明](packages/paper_trading_engine/README.md)：模拟交易引擎的配置、接口、运行边界
  和包级开发信息。
- [事故复盘索引](docs/incidents/README.md)：已确认运行事故的时间线、影响、修复证据和防复发措施。

### 策略研究

- [研究项目索引](research/README.md)：各策略研究线的阶段、交接入口和前瞻监测方案。
- [研究交接](docs/RESEARCH_HANDOFF.md)：实验工作流、数据污染规则、候选评估、冻结与继续研究方式。
- [OPC交易策略研究目标](research/RESEARCH_MANDATE.md)：通用收益、风险、仓位和证据目标。
- [实验档案说明](experiments/README.md)：实验目录、命名、产物契约和不可变归档规则。

### 开发与质量

- [技术交接](docs/DEVELOPMENT_HANDOFF.md)：架构契约、开发环境、代码入口、测试和恢复方法。
- [测试用例治理](docs/TEST_GOVERNANCE.md)：测试分层、边界覆盖和周期性治理规则。
- [Dataflows技术说明](packages/dataflows/README.md)：数据包接口和使用方式。
- [Strategy Evaluator技术说明](packages/strategy_evaluator/README.md)：候选评估与统计审计接口。

### 历史设计

- [`docs/superpowers/specs/`](docs/superpowers/specs/)：已批准设计说明。
- [`docs/superpowers/plans/`](docs/superpowers/plans/)：对应实施计划和历史执行路径。

历史设计文档用于审计设计演进，不代表当前操作入口。开始使用项目时先阅读用户使用说明；
继续策略研究时从研究项目索引进入；修改代码或运行测试时以技术交接为准。
