# Paper Trading Engine

PTE 是与 `czsc_trader` 并列的模拟交易运行包。PTE 通过 `czsc-trader advice run`
取得 `advice.v4` 决策，通过 Futu 中国市场模拟账户执行，并把虚拟账户账本与审计事件
保存在本机 SQLite。PTE 不导入 Trader、Strategy Manager 或 Strategy Evaluator。

## 对象关系

```text
冻结策略发布 1 ── 1 虚拟账户 N ── 1 Futu模拟渠道 ── 1 Futu模拟账户
                         │
                         └── 决策、意图、订单、成交、资金、持仓、绩效
```

- 虚拟账户是业务归属中心；每个账户绑定一个不可变策略发布和唯一渠道。
- 当前唯一渠道为 `futu`；一个 Futu 渠道承载多个虚拟账户。
- Futu 渠道只执行、回报和对账，不绑定策略，不生成决策，不修改价格或数量。
- 所有订单和成交必须能够追溯到虚拟账户、策略发布和决策。
- PTE 独占底层 Futu 模拟账户；无法归属的活动订单或不一致持仓会阻止新单。
- 内部 OHLC 模拟成交渠道已经移除；只有 Futu 累计成交回报能够改变账户持仓。

## 安装与启动

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
.\.venv\Scripts\pte.exe once --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte.exe serve --repo-root D:\CodeBase\czsc_trader
```

运行前启动 Futu OpenD，并确保只有一个中国市场模拟交易账户。PTE 只连接 Futu 交易
接口，不创建行情连接，也不探测或维护 Futu 行情权限状态；委托价格严格来自 Trader
决策。控制台地址为 <http://127.0.0.1:8080>。

PTE 每 5 秒同步订单和成交，每 60 秒同步渠道账户与持仓。每个自然日 19:00 后发布一次
完整收盘数据，失败按 5、15、30、60、300 秒退避。数据发布成功后，每个运行账户只
生成一次对应数据版本的决策；重启发现漏做时自动补做。新单仅在决策 `valid_session`
当天的 `09:30–11:30`、`13:00–14:57`（北京时间）提交。

`--decision-interval`仅为旧服务配置保留，已经不控制决策轮询。

## 虚拟账户

首次启动会幂等创建 `s001-v1 / S001-v1模拟账户`，绑定 `S001-v1`，初始资金10万元。
每个新增账户同样默认10万元；账户资金相互独立，底层 Futu 模拟账户提供共享容量。

```powershell
.\.venv\Scripts\pte.exe account list --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte.exe account create --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-v2 --name "S001-v2模拟账户" `
  --strategy S001 --strategy-version v2
.\.venv\Scripts\pte.exe account pause --repo-root D:\CodeBase\czsc_trader --account-id s001-v2
.\.venv\Scripts\pte.exe account resume --repo-root D:\CodeBase\czsc_trader --account-id s001-v2
```

账户身份包含账户ID、名称、标的、策略ID、版本、发布哈希、策略选择截止日和初始资金，
创建后不可变。
暂停只阻止该账户产生新订单，已有订单继续对账。买单创建意图时冻结本账户资金；明确
拒绝、过期或未完全成交后终止时释放剩余冻结资金。网络中断等结果不确定的提交保留
冻结资金并阻塞账户，等待按订单备注恢复归属。卖单不得超过该账户未被其他活动意图
占用的持仓。

## 控制台与审计

- `/accounts/{account_id}`：账户身份、最新决策、前瞻观察图、订单、成交、资金、持仓、
  绩效和开关。
- `/channels/futu`：底层 Futu 模拟账户、承载账户、逐笔订单/成交归属和撤单。
- `/comparison`：多个虚拟账户在共同观察区间内的只读比较。
- `/audit-events`：策略、交易、系统和其他事件，可按账户、策略、渠道和关联ID筛选。

策略事件归属虚拟账户；Futu 外部调用和渠道对账事件归属 Futu 渠道；交易事件同时带
账户与渠道。时间以 UTC 写入，页面统一显示北京时间。页面轮询采用稳定指纹，只在业务
数据变化时重绘。

“前瞻观察”默认展示选择截止日以前最后60个交易日和截止日后的全部完整日线。红色截止
线左侧仅作开发行情背景，右侧展示该账户实际持久化的信号、意图、成交、目标持仓和实际
持仓。PTE从运行数据目录读取日线并通过`account_observation.v1`的stdin/stdout契约调用：

```powershell
Get-Content account_observation.json -Raw |
  .\.venv\Scripts\czsc-trader.exe chart observation --format html
```

TDR只在内存中绘制，不读取PTE或Trader行情目录，也不保存前瞻行情和HTML。PTE将HTML
缓存在`state/paper_trading/charts/{account_id}/`；内容指纹不变时复用现有图表，绘图
失败可在账户卡片重试，且不会改变交易运行状态。

撤单必须选择归属账户和 Futu 订单，获取两分钟有效令牌并二次确认。未知活动订单、
账户汇总持仓与 Futu 持仓不一致、非 `SIMULATE/CN` 环境或分配资金超过底层总资产，
都会使渠道对账进入 `BLOCKED`。

## 模拟盘证据

```powershell
.\.venv\Scripts\pte.exe performance export --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-v2 --recorded-by tomxiao `
  --start 2026-09-04 --end 2026-12-04 --output state\paper-forward.json
.\.venv\Scripts\czsc-trader.exe strategy evidence add --input state\paper-forward.json
```

PTE 日常状态位于 `state/paper_trading/runtime.db`，行情副本和日志也位于
`state/paper_trading/`，均不进入 Git。跨机延续模拟盘需迁移整个运行目录。

## Windows watchdog

在管理员 PowerShell 中首次注册：

```powershell
.\.venv\Scripts\pte-watchdog.exe install-config --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte-watchdog.exe start --wait 30
```

服务名为 `CZSC-PTE-Watchdog`。日常发布代码后可使用 PTE 的本地控制接口优雅重启，
无需重启系统服务：

```powershell
.\.venv\Scripts\pte.exe control restart --repo-root D:\CodeBase\czsc_trader
```

## 最小回归

```powershell
.\.venv\Scripts\python.exe -m pytest packages\paper_trading_engine\tests\functional -q
node packages\paper_trading_engine\tests\functional\console_state.test.mjs
```
