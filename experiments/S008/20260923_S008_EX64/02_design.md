# S008 EX64 实验设计

对每个中国信号日，XAU/XAG通过`merge_asof(direction=backward, allow_exact_matches=false)`取严格
早于该日的最新FXCM报价，保留中国休市期间的外盘观察。输入合同继续使用`LATEST_AVAILABLE`且
最大陈旧7日。机器独立复算EX53口径的120日金银比偏离和20日金银相对收益，要求与运行时逐值
一致、源日期一致、逐日陈旧度不超过7日，并覆盖锚点FLAT/LONG状态。
