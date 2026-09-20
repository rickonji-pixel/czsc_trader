# 模块边界待办

本文只记录已确认、但不属于当前 DFLS 历史数据完整性分支的边界问题。当前分支不修改这些
职责，待 DFLS 边界完成并合并后单独评审和实施。

## SRT 与 PTE

- `packages/paper_trading_engine/src/paper_trading_engine/publication_inbox.py` 在 PTE 内解析并
  鉴真 SRT generation。发布物的读取、身份校验和可用状态应由 SRT 公共读取入口返回；PTE
  只保留活跃账户覆盖、跨标的截止日同步和调度状态判断。
- `packages/paper_trading_engine/src/paper_trading_engine/srt_advice_client.py` 同时读取 SRT 标准
  publication 和兼容性行情文件，并从行情文件提取截止日、下一交易日和参考价。后续应收敛
  为单一 SRT 运行入口。
- `packages/strategy_runtime/src/strategy_runtime/execution_planner.py` 当前由宿主传入信号价和
  执行参考价。价格、费率、目标仓位和委托参数属于 SRT，应直接取自 SRT publication 或决策。
- `packages/paper_trading_engine/src/paper_trading_engine/account_chart.py` 直接读取行情清单。后续
  应消费 SRT 提供的只读观测数据，避免 PTE 感知发布文件布局。
- `packages/paper_trading_engine/src/paper_trading_engine/release_cli.py` 的生产预检直接组合 PTE
  generation 校验和 SRT publication 校验。后续由 SRT 返回完整预检结果。

## TDR 与 SRT

- `src/czsc_trader/generation_integrity.py` 解释 SRT generation 的文件结构和哈希。后续应迁入
  SRT，由 TDR 只消费读取结果。
- `src/czsc_trader/application/data_service.py` 在 generation 提交后组合 TDR 文件校验、SRT
  publication 读取和运行时校验。后续需要区分 TDR 数据池事务与 SRT 发布物鉴真职责。
- `src/czsc_trader/backtesting/datasets.py` 和 `src/czsc_trader/backtesting/srt_bridge.py` 直接
  校验 generation。普通回测应通过 SRT 读取已发布输入，TDR 只校验研究窗口与返回截止日。

## 双轨发布

SRT 生产发布目前同时写入按 release 保存的标准 publication，以及供 PTE 使用的平面行情
兼容文件。两套文件都进入同一 generation，但 PTE 会分别读取并再次对齐。完成 SRT 统一读取
入口后，应评审兼容文件是否仍有保留价值，并消除双轨身份。
