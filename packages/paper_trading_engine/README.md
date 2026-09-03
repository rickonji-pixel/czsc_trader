# Paper Trading Engine

PTE 是与 `czsc_trader` 并列的模拟交易运行包。它通过 `czsc-trader advice run`
的 `advice.v4` JSON 契约取得正式策略版本决策，通过渠道适配器执行，并将运行状态和审计
事件保存到 SQLite。PTE 不导入 Trader 或 Strategy Manager，也不直接获取研究数据。

## 安装

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\packages\paper_trading_engine
```

运行前启动本机 Futu OpenD，确保存在中国市场模拟账户。行情权限可以缺失；页面
会显示 `DEGRADED_QUOTE`，自动提交仍按项目侧 advice 数值限价执行。渠道自动调价
固定关闭。

## 运行

先执行单轮诊断：

```powershell
.\.venv\Scripts\pte.exe once --repo-root D:\CodeBase\czsc_trader
```

确认结果后启动持续服务：

```powershell
.\.venv\Scripts\pte.exe serve --repo-root D:\CodeBase\czsc_trader
```

页面入口为 `http://127.0.0.1:8080`，控制台按资源分成三个页面：

- `/accounts/{account_id}`：虚拟账户工作区，账户、决策、模型订单、模型成交、绩效和开关统一归属该账户。
- `/channels/futu`：Futu模拟渠道，展示实际账户、显式绑定策略、渠道决策、订单、成交和撤单。
- `/comparison`：多个虚拟账户在共同观察区间内的只读比较。

订单和累计成交默认每 5 秒轮询，账户与持仓
每 60 秒刷新，策略每 5 秒检查一次数据身份，只有完整收盘数据身份或实际持仓发生
变化才重新调用 advice。Futu渠道异常按5、15、30、60、300秒退避，虚拟账户继续
独立运行；失败计数和下次重试时间保存在运行库，PTE重启不会清空退避状态。运行库位于 `state/paper_trading/runtime.db`；运行数据位于
`state/paper_trading/data`，均不进入版本控制。重启会复用订单意图、渠道订单、暂停
状态与审计事件。

服务在每个自然日 19:00 后自动运行一次数据发布，发布失败会写入告警事件并按退避
间隔重试。发布流程通过 Tushare 的 SSE 交易日历写入下一有效交易日，策略建议和
自动提交均使用该日期，不按普通工作日推断。各频率和发布时间可分别调整：

```powershell
.\.venv\Scripts\pte.exe serve --repo-root D:\CodeBase\czsc_trader `
  --order-interval 5 --account-interval 60 `
  --decision-interval 5 --data-refresh-time 19:00
```

自动买入使用模拟账户已对账的全部可部署现金。PTE 将现金与实际持仓交给
`czsc-trader advice run`，由项目侧按限价、执行费率和 100 份交易单位计算最大可买
数量；卖出信号卖出全部已成交持仓。渠道不参与定价或改量。

页面字段使用中文业务语义，英文枚举仍保留在 API、数据库和可展开的结构化诊断区。
暂停、恢复和撤单操作都会立即显示成功或失败反馈。

自动新单仅在决策的有效交易日，并处于 `09:30–11:30` 或 `13:00–14:57`
（Asia/Shanghai）时提交。夜间、午休和集合竞价阶段继续观测与对账。

## 虚拟账户

PTE首次启动会幂等创建绑定`S001 / 综合基线策略 / v1`和10万元初始资金的账户；
`baseline-143`只保留为内部兼容账户ID。每个虚拟账户拥有独立现金、持仓、成本、
意图、订单、成交和日快照。
19:00完整数据发布成功后，PTE先结算当日有效订单，再生成下一交易日决策；暂停只
阻止新订单，已有订单仍结算。开盘改善按开盘价成交，盘中严格穿价按限价成交，
等价触及记为不确定且不成交。

```powershell
.\.venv\Scripts\pte.exe account list --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte.exe account create --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-shadow --name "S001影子账户" `
  --strategy S001 --strategy-version v1 --futu-reference
.\.venv\Scripts\pte.exe account pause --repo-root D:\CodeBase\czsc_trader --account-id s001-shadow
.\.venv\Scripts\pte.exe account resume --repo-root D:\CodeBase\czsc_trader --account-id s001-shadow
```

控制台以URL中的虚拟账户为统一作用域，账户概览、最新策略决策、模型订单、
模型成交、账户详情和暂停开关同步切换，并在浏览器内记住下次入口偏好。Futu账户、
渠道订单和撤单操作集中在独立的Futu渠道页。多账户比较采用共同观察区间，
盈亏比仅统计已闭合买卖交易。`--futu-reference`用于切换唯一的Futu
参照账户，只影响页面标记和比较，不会向Futu复制虚拟订单。每个新账户默认初始资金
为10万元；可通过`--initial-cash`显式覆盖。Futu渠道账户的100万元不受此默认值影响。

模拟盘里程碑证据通过以下命令导出，随后交给Trader登记：

```powershell
.\.venv\Scripts\pte.exe performance export --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-shadow --recorded-by tomxiao `
  --start 2026-09-03 --end 2026-12-03 --output state\paper-forward.json
.\.venv\Scripts\czsc-trader.exe strategy evidence add --input state\paper-forward.json
```

证据包包含发布身份、统计口径和可复核的净值/闭合交易输入；PTE不直接写
`configs/strategies/`。

## Futu渠道策略绑定

冻结策略只创建独立虚拟账户。Futu模拟账户执行哪个正式策略，必须由操作者显式绑定：

```powershell
.\.venv\Scripts\pte.exe channel bind-strategy --repo-root D:\CodeBase\czsc_trader `
  --channel futu --strategy S001 --strategy-version v2 `
  --actor tomxiao --reason "人工确认切换Futu模拟执行策略"
```

PTE通过Trader CLI校验策略资格和发布哈希，随后把绑定写入SQLite并记录操作者、原因、
旧绑定和新绑定。相同发布重复绑定保持幂等；存在活动渠道订单时拒绝切换。首次升级
Console v2会从最近一次Futu渠道决策迁移绑定；无完整历史决策时保持未绑定，渠道继续
对账，同时阻止生成新决策和提交新单。

## Windows 服务

先在管理员 PowerShell 中注册并启动 watchdog：

```powershell
.\.venv\Scripts\pte-watchdog.exe install-config --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte-watchdog.exe start --wait 30
```

服务状态使用`sc.exe query CZSC-PTE-Watchdog`查看；管理命令为
`stop --wait 30`、`restart --wait 30`和`remove`，需管理员PowerShell。唯一的
系统服务名为 `CZSC-PTE-Watchdog`，启动类型为自动。watchdog 通过现有 `pte serve`
CLI 启动 PTE 子进程，每 10 秒检查进程和 8080 HTTP 状态；连续 3 次失败后重启，
退避间隔为 5、30、60 秒。安装时会停止并删除旧的 `CZSC-PaperTrading` 服务。

配置保存在 `state/paper_trading/service.json`；watchdog 滚动日志位于
`state/paper_trading/logs/watchdog.log`，PTE 输出位于
`state/paper_trading/logs/pte.log`。

## 干预语义

- 暂停只阻止新订单，已有订单继续对账。
- 恢复要求本进程已成功完成一次渠道对账。
- 撤单先获取绑定具体订单、两分钟有效的令牌，再做第二次确认。
- 只有渠道明确返回的累计成交数量增量会记录为成交；委托受理不更新持仓。
- 新单只在 advice 的`valid_session`当天提交；存在未终态订单时先等待订单收敛。

`pte once` 也可能自动提交 advice 给出的新订单。只读诊断前应先用
`czsc-trader advice run` 确认当前 `order` 为 `null`，或先在页面暂停新订单。
