# Paper Trading Engine

PTE 是与 `czsc_trader` 并列的模拟交易运行包。它通过 `czsc-trader advice run`
的 `advice.v1` JSON 契约取得策略决策，通过渠道适配器执行，并将运行状态和审计
事件保存到 SQLite。PTE 不导入策略包，也不直接获取研究数据。

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
.\.venv\Scripts\pte.exe once --repo-root D:\CodeBase\czsc_trader --position-size 50000
```

确认结果后启动持续服务：

```powershell
.\.venv\Scripts\pte.exe serve --repo-root D:\CodeBase\czsc_trader --position-size 50000
```

页面地址为 `http://127.0.0.1:8765`，默认每五秒完成一次账户、持仓和订单对账。
运行库位于 `state/paper_trading/runtime.db`。重启会复用该库中的订单意图、渠道订单、
暂停状态与审计事件。

## 干预语义

- 暂停只阻止新订单，已有订单继续对账。
- 恢复要求本进程已成功完成一次渠道对账。
- 撤单先获取绑定具体订单、两分钟有效的令牌，再做第二次确认。
- 只有渠道明确返回的累计成交数量增量会记录为成交；委托受理不更新持仓。
- 新单只在 advice 的`valid_session`当天提交；存在未终态订单时先等待订单收敛。

`pte once` 也可能自动提交 advice 给出的新订单。只读诊断前应先用
`czsc-trader advice run` 确认当前 `order` 为 `null`，或先在页面暂停新订单。
