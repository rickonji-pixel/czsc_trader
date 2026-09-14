# S001 EX18 结论

S001-v2完整开发池年化26.75%、最大回撤-21.53%、卡玛1.242。控制组通过TDR正式账户引擎精确复现。

## 主要结论

1. 12项输入的剔除审计得到8项`SUPPORTIVE`、4项`REGIME_DEPENDENT`，没有`REDUNDANT`或`HARMFUL`。在当前冻结结构和开发池内，没有证据支持删除任何输入。
2. 4个信息族得到2个`SUPPORTIVE`、2个`REGIME_DEPENDENT`。市场结构与趋势动量是稳定支柱；量能流动性与位置估值具有明显阶段依赖。
3. F04, F05, F11, F12在较差的60交易日窗口内可出现零次正状态，但剔除后组合表现仍有实质下降。频率不能作为S001组合输入的质量门槛，只适合作为观察指标。
4. 12项单输入中有0项独立满足收益与15%回撤双门。S001-v2的表现来自多信息族组合、权重和regime分配，不能归因于某一个单独信号。

## 单输入剔除

|输入|信息族|剔除后年化差|剔除后回撤差|剔除后卡玛差|Bootstrap年化/卡玛|标签|
|---|---|---:|---:|---:|---:|---|
|F01|MARKET_STRUCTURE|7.51%|4.38%|0.500|96.9%/95.9%|REGIME_DEPENDENT|
|F02|MARKET_STRUCTURE|3.57%|-0.19%|0.156|83.3%/74.1%|REGIME_DEPENDENT|
|F03|MARKET_STRUCTURE|14.34%|19.73%|0.942|99.6%/99.4%|SUPPORTIVE|
|F04|MARKET_STRUCTURE|10.16%|7.30%|0.667|100.0%/100.0%|SUPPORTIVE|
|F05|MARKET_STRUCTURE|7.60%|3.07%|0.464|99.9%/99.6%|SUPPORTIVE|
|F06|TREND_MOMENTUM|7.00%|7.07%|0.552|94.3%/93.3%|SUPPORTIVE|
|F07|TREND_MOMENTUM|13.97%|15.66%|0.899|98.7%/99.4%|SUPPORTIVE|
|F08|TREND_MOMENTUM|9.23%|19.22%|0.812|98.7%/97.3%|SUPPORTIVE|
|F09|TREND_MOMENTUM|12.12%|20.59%|0.895|100.0%/100.0%|SUPPORTIVE|
|F10|VOLUME_LIQUIDITY|18.09%|14.22%|1.000|98.0%/96.7%|REGIME_DEPENDENT|
|F11|POSITION_VALUATION|4.50%|1.53%|0.277|97.4%/96.5%|REGIME_DEPENDENT|
|F12|MARKET_STRUCTURE|5.62%|0.02%|0.262|97.1%/94.3%|SUPPORTIVE|

## 信息族剔除

|信息族|剔除后年化差|剔除后回撤差|剔除后卡玛差|标签|
|---|---:|---:|---:|---|
|MARKET_STRUCTURE|17.65%|22.58%|1.036|SUPPORTIVE|
|TREND_MOMENTUM|23.84%|29.94%|1.186|SUPPORTIVE|
|VOLUME_LIQUIDITY|18.09%|14.22%|1.000|REGIME_DEPENDENT|
|POSITION_VALUATION|4.50%|1.53%|0.277|REGIME_DEPENDENT|

## 解释边界

剔除实验会把剩余权重重新归一化，并保持原阈值、regime和执行规则不变；因此差异同时包含输入缺失与权重再分配的影响，只能解释为组合内边际证据，不能解释为单因子因果收益。全部结果来自S001-v2原开发池，不能充当前瞻证据。实验不授权删除输入、修改S001-v2或生成候选。
