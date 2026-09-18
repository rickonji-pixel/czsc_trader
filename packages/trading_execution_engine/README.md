# Trading Execution Engine（TXE）

TXE 是统一成交执行器，位于 DFLS 同一基础能力层。它接收SRT决策及执行计划，生成可审计的
订单、成交、费用、现金、持仓和净值事实，供研究复算、候选回测和 TDR 冻结体检共同使用。

## 能力边界

TXE 负责：

- 市价单和限价单的统一触发与成交价格规则；
- 对交易者不利的滑点处理；
- 目标仓位到买卖数量的转换；
- 费用、现金、持仓和净值的确定性记账；
- 对非法价格、数量、费率、滑点和目标仓位明确失败。

TXE 不负责：

- 生成策略信号或目标仓位；
- 获取、发布或修补行情数据；
- 搜索参数或评价策略优劣；
- 管理策略版本、PTE账户或券商订单状态。

PTE仍以渠道成交回报作为模拟账户事实来源。TXE用于研究、回测和冻结复核的统一执行口径，
不能替代Futu订单与成交对账。

## 当前接口

- `HistoricalExecutor`：实现SRT的`ExecutionChannel`协议，管理历史执行、账户状态、幂等请求
  和完整账本；研究参数搜索、候选/冻结策略回测、TDR节点二复核共用这条执行链路；
- `resolve_fill(...)`：按统一市价/限价规则判断单笔委托是否成交；
- `execute_target_positions(...)`：按交易日开盘执行多空受限为`[0, 1]`的目标仓位，并生成订单、
  费用和账户日状态。

后两者是数值能力接口；正式策略回放经SRT与`HistoricalExecutor`执行，不能用简化仓位收益
替代完整执行证据。策略决定委托类型、价格和时点，TXE按声明规则执行。原`BacktestChannel`
已移除，TDR负责工作流编排、报告和图表。

限价触碰默认采用保守的严格穿越规则，以保持现有正式回测口径；如调用方明确需要“触价即成交”，
必须显式传入`inclusive_touch=True`并在证据中记录。

## 验证

```powershell
.\.venv\Scripts\python.exe -B -m pytest -c pyproject.toml packages/trading_execution_engine/tests -q
```
