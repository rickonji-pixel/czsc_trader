# CZSC Trader / CZSC PTE

这是一个面向个人量化团队的可审计策略研发与模拟交易仓库。项目覆盖受控行情、策略研究、
候选评估、不可变策略版本、确定性回测、交易决策、虚拟账户模拟交易和运行审计。

## 核心模块

| 模块 | 简称 | 位置 | 职责 |
| --- | --- | --- | --- |
| CZSC Trader | TDR | `src/czsc_trader/` | 数据、策略解析、回测、研究编排和交易决策 |
| Strategy Manager | SM | `packages/strategy_manager/` | 策略身份、版本、资格、证据和治理审计 |
| Strategy Evaluator | SE | `packages/strategy_evaluator/` | 候选筛选、排名、体检和统计稳健性审计 |
| Paper Trading Engine | PTE | `packages/paper_trading_engine/` | 虚拟账户、模拟交易、运行审计和控制台 |
| PTE Watchdog | WDG | PTE包内 | PTE进程开机自启、探活和故障拉起 |
| Dataflows | — | `packages/dataflows/` | Tushare行情获取、复权、多频发布和清单 |

TDR通过`advice.v4` JSON契约向PTE提供交易决策。PTE通过CLI调用TDR，不导入TDR、SM
或SE；Futu渠道只负责执行、回报和对账，不参与策略计算、定价或改量。WDG只管理PTE
进程生命周期和健康探测，不持有交易业务配置。

## 文档入口

| 需求 | 文档 |
| --- | --- |
| 安装、数据更新、回测、策略查看、PTE控制台和WDG/PTE启停 | [用户使用说明](docs/USER_GUIDE.md) |
| 继续策略研究、创建实验、评估候选、冻结策略和防止数据污染 | [研究交接](docs/RESEARCH_HANDOFF.md) |
| 查看S001、S002等各条研究线的当前状态和下一步 | [研究项目索引](research/README.md) |
| 继续代码开发、理解架构契约、恢复环境和执行测试 | [技术交接](docs/DEVELOPMENT_HANDOFF.md) |
| 控制TDD用例膨胀、收敛测试并执行周期性治理 | [测试用例治理](docs/TEST_GOVERNANCE.md) |

包级技术参考：

- [Dataflows](packages/dataflows/README.md)
- [Strategy Evaluator](packages/strategy_evaluator/README.md)
- [Paper Trading Engine](packages/paper_trading_engine/README.md)

已批准设计与实施计划保存在`docs/superpowers/`，用于历史审计，不作为当前使用入口。

## 当前运行基线

- Python 3.12；
- 当前正式策略身份：`S001 / 综合基线策略`；
- 当前模拟交易渠道：Futu中国市场模拟交易；
- PTE控制台：<http://127.0.0.1:8080>；
- Windows服务：`CZSC-PTE-Watchdog`；
- `data/raw/`为受控研究池，`data/backtest/`为独立普通回测数据集。

## 最短开始路径

```powershell
git clone https://github.com/tomxiao/czsc_trader.git czsc_trader
cd czsc_trader
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\czsc-trader.exe strategy list
```

完整依赖安装和所有操作命令以[用户使用说明](docs/USER_GUIDE.md)为准。研究和开发接手前，
分别阅读对应的交接文档。
