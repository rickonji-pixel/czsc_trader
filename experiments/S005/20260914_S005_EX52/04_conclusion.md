# S005 EX52 结论

裁决：`EXPAND_INFORMATION_COVERAGE_BEFORE_RETURN_TEST`。EX49的136项是信号状态，折叠后只有68个信号配置；粗分类为OTHER_TECHNICAL 12个、STRUCTURE 17个、TREND_MOMENTUM 32个、VOLUME_FLOW 7个。全部机会信号均来自30分钟周期。

EX50原型只覆盖结构与量价效率两个粗信息族，且只有30分钟尺度。它可以作为最小机制探针，但不足以直接对标S001-v2的三类信息、三种时间尺度与ER60 regime完成态。暂停该原型的收益测试；下一轮先对信号做人工语义分类，并从技术合格但频率较低的日线、周线状态中寻找环境层。频率门只约束机会与最终闭合交易，不约束regime本身。

## 状态行为最相近的信号对

|左信号|右信号|NMI|近似冗余|
|---|---|---:|---|
|`zdy_macd_dif_iqr_V230521`|`tas_macd_base_V221028`|0.991|是|
|`tas_macd_base_V221028`|`tas_macd_power_V221108`|0.667|否|
|`zdy_macd_dif_iqr_V230521`|`tas_macd_power_V221108`|0.664|否|
|`jcc_fen_shou_xian_V20221113`|`bar_classify_V240607`|0.654|否|
|`ntmdk_V230824`|`bar_section_momentum_V221112`|0.634|否|
|`tas_ma_base_V221101`|`tas_ma_base_V230313`|0.561|否|
|`xl_bar_trend_V240330`|`tas_ma_system_V230513`|0.543|否|
|`tas_double_ma_V221203`|`tas_ma_system_V230513`|0.541|否|
|`byi_fx_num_V230628`|`cxt_bi_end_V230618`|0.522|否|
|`byi_fx_num_V230628`|`cxt_bi_base_V230228`|0.510|否|

NMI只衡量状态序列依赖程度，不证明金融机制相同。`OTHER_TECHNICAL`仍是待人工拆分的临时标签。本轮没有创建候选，没有修改SM或PTE。
