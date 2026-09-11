# S003 EX66 设计

沿用EX65的开发池和变量。事件日与非事件日必须属于同一自然年，采用不放回一对一最优
匹配；前一日收益差不得超过全样本标准差的0.10，合法组合再按前一日收益、前5日收益、
20日波动率、60日趋势、成交活跃度和开盘缺口的标准化欧氏距离最小化。

匹配质量门槛固定为：至少保留70%的事件，任一配对的前一日收益差不超过0.10个标准差，
前一日收益绝对SMD不超过0.02，六个连续变量最大绝对SMD不超过0.10。任一门槛失败，结论
固定为`MATCHING_QUALITY_FAILED`。

通过匹配门后，对配对收益差按事件日所在自然月进行1,000次区块Bootstrap。90%区间下界
大于0，记为`MONEYFLOW_INCREMENT_SUPPORTED_IN_COMMON_SUPPORT`；区间覆盖0，记为
`INCREMENT_UNRESOLVED_IN_COMMON_SUPPORT`；区间上界不大于0，记为
`MOMENTUM_EXPLANATION_SUPPORTED_IN_COMMON_SUPPORT`。结论仅适用于共同支持子样本。
