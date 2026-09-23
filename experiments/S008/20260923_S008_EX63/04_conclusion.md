# S008 EX63 结论

机器裁决：`STOP_CROSS_MARKET_ALIGNMENT_SEMANTICS`。EX53信息审计使用严格向后`merge_asof`，即每个
中国信号日取严格早于该日的最新外盘报价；当前运行时没有保持这一口径。后继实验必须恢复EX53
的严格前值对齐，再重新验证陈旧度、特征逐值一致和状态机。
