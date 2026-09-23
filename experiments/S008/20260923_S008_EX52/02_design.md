# S008 EX52 实验设计

## 继承关系

本实验继承`20260923_S008_EX51`的六项优先能力、2003-01-02至2024-12-31边界和因果规则，
只验证平台提交`2d398a9b`补齐的DFLS能力。EX51的
`PLATFORM_ADAPTATION_REQUIRED_GLOBAL_RISK_INPUTS`结论保持不变。

## 数据合同

- `fx.fxcm_daily`：`XAUUSD.FXCM`、`XAGUSD.FXCM`；
- `index.vix_daily`：`VIX`；
- `index.global_daily`：`SPX`、`RUT`、`IXIC`；
- 六项数据必须精确覆盖2003-01-02至2024-12-31，至少5,400行；
- 每项必须由DFLS返回`READY`，主键无重复，关键字段为有限数值且无空值；
- 每项必须生成Tushare来源、供应商代码、实际边界和内容SHA-256；
- FXCM源日期必须严格早于中国决策交易日，美国指数和VIX收盘日也必须严格早于中国决策交易日；
- 平台必须声明最大起点容忍10个自然日，防止供应商行数上限形成“假完整”；
- 只归档字段、行数、边界、身份和元数据摘要，不归档供应商原始数据。

## 裁决

1. 任一DFLS请求非`READY`：`FAIL_GLOBAL_RISK_DFLS_PUBLICATION_GATE`；
2. 任一覆盖、主键、字段或身份检查失败：`FAIL_GLOBAL_RISK_DFLS_DATA_GATE`；
3. 任一因果元数据不匹配：`FAIL_GLOBAL_RISK_CAUSALITY_GATE`；
4. 全部通过：`PROCEED_TO_GLOBAL_RISK_INFORMATION_AUDIT`。

无论裁决如何，本实验都不形成Alpha、策略、参数或候选证据。
