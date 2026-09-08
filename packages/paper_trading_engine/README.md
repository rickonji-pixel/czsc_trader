# Paper Trading Engine

PTE是仓库内独立的模拟交易包，与TDR通过CLI和版本化JSON契约协作。本文只描述包级边界、
对象模型、技术契约和开发验证。安装、账户操作、控制台使用以及PTE/WDG启停见
[用户使用说明](../../docs/USER_GUIDE.md)。

## 职责与边界

PTE负责：

- 虚拟账户、资金分账、订单意图、成交回报和绩效快照；
- 完整收盘数据发布、逐账户决策调度和有效交易时段提交；
- Futu模拟交易执行与对账；
- SQLite追加式审计、控制台和账户前瞻观察图；
- 向SM导出人工确认所需的模拟盘里程碑证据。

PTE不负责策略计算、价格调整、目标仓位或委托数量计算，这些事实由TDR通过`advice.v4`
提供。PTE不导入TDR、SM或SE，也不创建或探测Futu行情连接。内部OHLC模拟成交渠道已经
移除；只有Futu累计成交回报能够改变账户现金和持仓。

WDG位于本包内，只管理PTE子进程生命周期和HTTP健康探测。数据发布时间、OpenD连接、
调度周期和交易规则均由PTE持有，WDG不得向PTE注入业务参数。

## 对象关系

```text
StrategyRelease 1 ─── N VirtualAccount
                         │
                         ├── N Decision
                         ├── N OrderIntent ─── N FutuFill
                         └── N AccountSnapshot

FutuChannel 1 ─── N VirtualAccount
FutuChannel 1 ─── 1 Futu SIMULATE/CN account
```

- 一个虚拟账户绑定一个不可变策略发布和一个渠道；
- Futu渠道可承载多个虚拟账户，渠道本身不绑定策略；
- 每条决策、订单和成交必须追溯到虚拟账户、策略发布和关联ID；
- 未知活动订单或底层持仓与账户汇总不一致时，渠道阻止新单；
- 委托受理不代表成交，没有明确成交增量时保持账户账本不变。

## 外部契约

### TDR

- `advice.v4`：PTE传入标的、策略版本、实际持仓和可用资金，TDR返回确定性决策、目标
  数量、执行限价和订单列表；
- `account_observation.v1`：PTE通过stdin传入有限行情、决策和账户事实，TDR在内存中
  返回HTML，不读取或保存PTE前瞻行情；PTE使用外置Plotly运行库并长期缓存，账户数据
  刷新时保留图表DOM，只有图表事实变化才加载新的轻量HTML；
- PTE只接受冻结且资格为`PAPER_READY`的策略发布。

### Futu

- 固定使用`TrdEnv.SIMULATE`与中国市场模拟账户；
- PTE独占底层账户，所有虚拟账户订单共享该渠道容量；
- 订单状态和`dealt_qty`用于增量对账，明确回报前不改变账本；
- Futu不参与策略计算、定价和改量。

### SM

PTE日常运行状态不进入SM。只有人工复核后的里程碑通过自包含证据包写入SM；策略资格、
虚拟账户状态和PTE进程状态彼此独立。

## 运行结构

- `cli.py`：`once`、`serve`、账户、绩效导出和控制接口；
- `scheduler.py`：数据发布、退避、逐账户决策及订单/账户对账节奏；
- `coordinator.py`与`account_engine.py`：账户中心编排和账本；
- `futu_gateway.py`与`futu_execution.py`：Futu交易适配与执行；
- `store.py`与`audit.py`：SQLite状态及四类追加式审计事件；
- `web.py`：localhost控制台和HTTP控制接口；
- `account_chart.py`：账户前瞻观察图缓存；
- `watchdog.py`与`windows_service.py`：无业务逻辑的进程保活层。

本机运行数据库、行情副本、图表缓存和日志位于`state/paper_trading/`，均不进入Git。
跨机延续同一模拟盘序列需要迁移完整运行目录，并重新核对Futu活动订单、成交和持仓。

## 包级开发

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests -q
node --test packages\paper_trading_engine\tests\functional\console_state.test.mjs
.\.venv\Scripts\python.exe -m ruff check `
  packages\paper_trading_engine\src packages\paper_trading_engine\tests
```

长期测试保持少量完整功能场景。开发中的聚焦TDD用例在行为并入功能场景后删除，避免按
内部实现细节扩张测试数量。
