# 研究设计

## 1. 证据链

研究对象为`experiments/0824_EX04/artifacts/frozen_challenger.json`及其12个冻结信号和权重。事件结果来自受跟踪的`experiments/0825_EX04`正式档案；`0825_EX04`已验证其事件身份继承自`0825_EX03`。正式运行先验证`0825_EX04`清单及协议声明的源文件哈希，再验证`0824_EX04`冻结对象哈希。

## 2. 因果轨迹

对20个事件分别截取信号日及之前20个交易日，共最多21行。任何特征的最大日期必须不晚于该事件信号日。原始信号由仓库现有`generate_factor_frame`因果生成，使用`0824_EX04`冻结的12个信号身份、顺序与权重；2026文件名一旦进入行情哈希立即失败。

每个信号生成以下原子描述符：

- `primary_at_t`：信号日CZSC主状态文本；
- `mapped_at_t`：冻结语义映射后的-1、0或+1；
- `run_length_bin`：当前状态连续长度分箱`1`、`2_3`、`4_5`、`6_10`、`11_20`、`21_plus`；
- `last_transition`：最近一次状态变化的`前值=>后值`，20日内无变化为`no_transition`；
- `transition_age_bin`：距最近变化日分箱`0_1`、`2_3`、`4_5`、`6_10`、`11_20`、`none`；
- `flips_5_bin`、`flips_10_bin`、`flips_20_bin`：变化次数分箱`0`、`1`、`2_plus`；
- `contribution_delta_1_sign`、`contribution_delta_5_sign`：冻结加权贡献变化符号`negative`、`zero`、`positive`。

策略路径只增加固定的聚合描述符：冻结总分在`T/T-1/T-3/T-5`的变化符号、相对`0824_EX04`离场阈值的距离符号、最近5/10日低于或等于离场阈值的天数分箱，以及30分钟/日线/周线笔方向在T日的`all_same/two_same/all_different_or_neutral`一致性。不得增加价格技术指标或临时特征。

## 3. 顺序发现与确认

原子签名定义为`(factor, descriptor, value)`；聚合签名使用`factor=__strategy__`。只允许单个签名，不做AND/OR组合。

1. 发现阶段只查看两个2021病灶，保留两者值完全相同且非缺失的签名；
2. 确认阶段检查2023病灶是否具有相同签名；
3. 反例阶段统计该签名在其余17个事件、11个保护性离场和保护性`joint_margin_block`近邻中的支持；
4. 常量签名若在20个事件中全部相同则剔除；
5. 输出全部签名，不选择“最佳”签名，不根据结果修改分箱或门槛。

## 4. 机器分类

按以下顺序分类：

1. `insufficient_anatomy_evidence`：事件不是20个、病灶身份漂移、轨迹越过信号日、12个信号身份不匹配、存在非有限数值或2026哈希；
2. `shared_confirmed_low_contamination_anatomy`：至少一个发现签名通过2023确认，保护性支持不超过2/11，且保护性`joint_margin_block`支持为0；
3. `shared_but_contaminated_anatomy`：至少一个签名覆盖三个病灶，但没有签名达到低污染门槛；
4. `discovery_only_anatomy`：至少一个签名覆盖两个2021病灶，但没有签名通过2023确认；
5. `idiosyncratic_dominant_events`：没有非恒定原子签名同时覆盖两个发现病灶。

“共同病灶假设”只对应第2类。第3—5类均不准设计候选。

## 5. 图形附录

生成一个轻量SVG，分别展示三个病灶`T-20…T`的归一化收盘价、`0824_EX04`冻结总分和离场阈值，并以虚线标记信号日。图形只使用机器判定已经允许的T日前数据，不展示事后路径，不参与分类。

## 6. 产物与审计

正式产物固定为：

- `artifacts/protocol.json`；
- `artifacts/identity_audit.json`；
- `artifacts/event_signal_trajectory.csv`；
- `artifacts/event_descriptor_matrix.csv`；
- `artifacts/discovered_atomic_signatures.csv`；
- `artifacts/confirmed_atomic_signatures.csv`；
- `artifacts/anatomy_classification.json`；
- `artifacts/dominant_event_panels.svg`；
- `artifacts/metrics.json`。

正式执行只运行一次。结果无论支持还是否定共同机制，都按原分类如实归档，不触碰2026。
