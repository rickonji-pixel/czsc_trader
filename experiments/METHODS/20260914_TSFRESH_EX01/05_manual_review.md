# TSFRESH EX01 人工目录审查

机器门产生3项FSC人工审查提案。`20日成交额变化最小值`与`20日成交量变化最小值`在完整
特征区间的相关系数为0.9992，二者表达同一成交活跃度收缩信息；成交额还混入价格变化，
因此只保留语义更纯的成交量版本。

最终以`DISCOVERED`状态登记两项定义：

- `F-TSFRESH-VOLUME-CONTRACTION-FLOOR-20`，归属`VOLUME_LIQUIDITY`；
- `F-TSFRESH-ABS-RETURN-MAX-60`，归属`VOLATILITY_RISK`。

登记只表示定义、计算方法和信息族可被后续研究引用。510500开发期的方向证据不构成跨标的
Alpha结论；进入`READY`前仍需完成独立实现复核、跨标的适用性和经济语义审查。
