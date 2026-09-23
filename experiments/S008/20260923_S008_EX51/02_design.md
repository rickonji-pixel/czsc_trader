# S008 EX51 设计

## 优先能力

- 全球贵金属：`XAUUSD.FXCM`、`XAGUSD.FXCM`；
- 全球恐慌：Tushare `vix_index`；
- 全球权益风险：`SPX`、`RUT`、`IXIC`。

全部能力必须覆盖2003-01-02至2024-12-31、无重复日期、核心价格完整。FXCM和美国市场信息只能
映射到严格晚于其源日期的中国交易日。

## 对照与排除

`USDOLLAR.FXCM`、`USOil.FXCM`和`Copper.FXCM`仅验证停止日期，不参与优先能力通过判定。
预注册前目录探测已确认Tushare期货接口只覆盖境内交易所，未发现COMEX；美股证券目录没有GLD、
SLV等ETF。两类缺失只作为能力边界，不调用其他数据源补齐。

## 裁决

- 任一优先Tushare能力失败：`STOP_GLOBAL_RISK_DATA_ROUTE`；
- 优先能力全部可用，但DFLS缺少数据集、长请求被供应商行数上限截断或因果元数据不完整：
  `PLATFORM_ADAPTATION_REQUIRED_GLOBAL_RISK_INPUTS`；
- 优先能力和DFLS全部通过：`PROCEED_TO_GLOBAL_RISK_INFORMATION_AUDIT`。
