# S005 EX58 执行

状态：`COMPLETE`。13个项目级因子全部完成物化或事件入账，未读取后续收益。

|因子|信息族|观测|数值|覆盖|独立增量|既有S005证据|
|---|---|---:|---:|---:|---|---|
|F-PROJECT-ER60|TREND_REGIME|1347|1347|100.0%|否|S001 regime component; not an independent S005 alpha|
|F-PROJECT-FIRST-HOUR-RETURN|TREND_MOMENTUM|1408|1408|100.0%|否|not independently tested for S005|
|F-PROJECT-FIRST-HOUR-VWAP-DEVIATION|POSITION_VALUATION|1408|1408|100.0%|否|not independently tested for S005|
|F-PROJECT-FIRST-HOUR-PATH-EFFICIENCY|MARKET_MICROSTRUCTURE|1408|1408|100.0%|否|not independently tested for S005|
|F-PROJECT-FIRST-HOUR-ACTIVITY|VOLUME_LIQUIDITY|1388|1388|100.0%|否|not independently tested for S005|
|F-PROJECT-SIGNED-VOLUME-IMBALANCE|MARKET_MICROSTRUCTURE|1408|1408|100.0%|否|not independently tested for S005|
|F-PROJECT-BREADTH-BALANCE|MARKET_BREADTH|1353|1353|100.0%|是|EX21 standalone family failed return gates|
|F-PROJECT-BREADTH-THRUST-5|MARKET_BREADTH|1349|1349|100.0%|是|EX21 standalone family failed return gates|
|F-PROJECT-MONEYFLOW-BREADTH|FUND_FLOW|1353|1353|100.0%|是|EX21 standalone family failed return gates|
|F-PROJECT-ETF-SHARE-CHANGE|FUND_FLOW|1372|1372|100.0%|是|EX28 standalone family failed return gates|
|F-PROJECT-ETF-NAV-PREMIUM|POSITION_VALUATION|1372|1372|99.9%|是|EX33 standalone family failed return gates|
|F-PROJECT-EARNINGS-ACCELERATION-BREADTH|FUNDAMENTAL_EXPECTATION|205|205|18.2%|是|EX44 standalone mechanism failed density gate without reading returns|
|F-PROJECT-STRUCTURED-NEWS-EVENT|EVENT_CATALYST|86|0|1.1%|是|EX41 generic positive continuation failed; event data remains reusable|
