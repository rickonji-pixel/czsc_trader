# 研究设计

## 1. 证据链与新颖性

研究对象是`experiments/0824_EX04/artifacts/frozen_challenger.json`。20个事件、结果标签、市场状态和阻断类型来自受跟踪的`experiments/0825_EX04`正式档案；`0825_EX05`正式档案用于证明现有12信号轨迹已经被否定为充分表示。

正式运行必须验证上述档案清单和协议哈希，并读取`experiments/0824_EX06/artifacts/factor_candidates.csv`作为排重清单。EX06已有候选主要是CZSC类别状态及其展开；本轮只研究连续几何、能量和量价幅度，不把类别状态改名冒充新因子。

## 2. 因果数据口径

- 日线描述符在交易日T收盘后计算，最早只能影响T之后的交易；
- 30分钟描述符只使用T日及此前已经完成的30分钟K线；
- 周线描述符只使用截至T已经完成的周线，未完成周线不得进入计算；
- 复权、异常交易日修正和行情身份沿用受跟踪manifest；
- 任何产物行情日期晚于`2025-12-31`、任何特征最大时间晚于事件T，立即分类为证据不足并停止。

除特别说明外，`ret_n=C_t/C_{t-n}-1`；真实波幅`TR=max(H-L, |H-C_{t-1}|, |L-C_{t-1}|)`；`ATR_n`为TR的n日算术均值；分母为0时记缺失，不做无穷值替换。

## 3. 固定描述符

### 3.1 趋势几何（8项）

1. `close_to_ma20`：`C/MA20-1`；
2. `ma20_slope5`：`MA20_t/MA20_{t-5}-1`；
3. `ma10_ma20_spread`：`MA10/MA20-1`；
4. `drawdown_from_high20`：`C/max(H,20)-1`；
5. `close_location20`：`(C-min(L,20))/(max(H,20)-min(L,20))`；
6. `return5`：`ret_5`；
7. `return10`：`ret_10`；
8. `prior_low10_buffer`：`C/min(L_{t-10:t-1})-1`。

### 3.2 回撤能量（8项）

9. `atr5_to_atr20`：`ATR5/ATR20`；
10. `tr_to_atr20`：`TR_t/ATR20`；
11. `realized_vol5_to20`：5日收益标准差/20日收益标准差；
12. `downside_semivol5_to20`：5日负收益均方根/20日负收益均方根；
13. `negative_day_share5`：最近5日负收益日数/5；
14. `max_drawdown5_to_atr20`：最近5日收盘路径最大回撤绝对值除以`ATR20/C`；
15. `daily_close_location`：`(C-L)/(H-L)`；
16. `gap_abs_to_atr20`：`abs(O/C_{t-1}-1)/(ATR20/C_{t-1})`。

### 3.3 量价结构（7项）

17. `volume5_to20`：`mean(V,5)/mean(V,20)`；
18. `volume_t_to20`：`V_t/mean(V,20)`；
19. `down_up_volume_ratio10`：最近10日负收益日成交量均值/正收益日成交量均值；
20. `signed_volume_imbalance5`：最近5日`sum(sign(ret)*V)/sum(V)`；
21. `signed_volume_imbalance10`：最近10日同口径不平衡；
22. `return_volume_corr10`：最近10日收益与`log1p(V)`的Pearson相关；
23. `log_volume_slope5`：最近5日`log1p(V)`对固定时间索引0—4的最小二乘斜率。

### 3.4 日内与周线结构（5项）

24. `intraday_down_volume_share`：T日30分钟负收益K线成交量/当日全部30分钟成交量；首根以开盘到收盘收益定号，其余以相邻收盘收益定号；
25. `intraday_realized_vol_to20`：T日30分钟收益平方和平方根/此前20个完整交易日同口径中位数；
26. `intraday_close_location`：T日收盘在当日30分钟最高价与最低价区间中的位置；
27. `last4_30m_return`：T日最后4根完整30分钟K线的首开至末收收益；
28. `weekly_close_to_ma10`：最近完整周线收盘/最近10根完整周线收盘均值-1。

不得增加交互项、AND/OR组合、替代公式或临时描述符。实现若发现某公式无法按上述口径唯一计算，应在正式运行前回到设计评审，不得自行改义。

## 4. 因果历史分位箱

每项日线或日内描述符在事件T的值，只与该描述符在T之前252个交易日的历史值比较，最少要求120个有限历史值；分位点按固定的20%、40%、60%、80%切分为`Q1`—`Q5`，边界值按右闭规则进入较低分位箱。周线描述符使用T之前26根完整周线，最少要求20个有限值。

当前事件值不得参与自身分位点计算。缺失记为`NA`，`NA`不能成为发现签名。除历史分位箱外，同时输出原始连续值供审计，但机器主判定只读取分位箱。

## 5. 顺序发现、确认与反例

原子签名固定为`(descriptor, quantile_bin)`：

1. 发现阶段只保留两个2021发现事件分位箱完全一致的签名；
2. 时间确认阶段检查2023确认事件是否处于同一分位箱；
3. 反例阶段统计签名在11个保护性离场、下跌状态保护性离场和保护性`joint_margin_block`近邻中的覆盖；
4. 其余2个错误离场和4个中性离场只报告覆盖，不参与选择或门槛；
5. 不按原始值选择方向，不合并相邻分位箱，不根据结果修改历史窗口或分箱。

## 6. 精确多重比较校正

主判定材料集固定为三个主导错误离场和11个保护性离场，共14个事件。枚举其中任意3个事件作为伪主导标签的全部364种组合；对每种组合，在冻结的28项分位签名上重复“任一签名覆盖三个伪主导、最多覆盖其余2个伪保护、且不覆盖伪保护中的下跌状态和`joint_margin_block`事件”判定。

精确p值为满足该判定的伪标签组合数除以364。真实标签必须先满足单签名门槛，且精确p值不高于0.05，才允许判为确认。不得只报告未经校正的单因子p值。

## 7. 机器分类

按以下顺序分类：

1. `insufficient_new_representation_evidence`：身份、时间、事件数、描述符数、有限性、历史长度、哈希或2026隔离任一审计失败；
2. `new_representation_confirmed`：至少一个签名满足全部覆盖、污染和精确p值门槛；
3. `suggestive_but_multiplicity_unconfirmed`：存在满足覆盖及污染门槛的签名，但精确p值大于0.05；
4. `shared_but_contaminated_new_representation`：至少一个签名覆盖三个主导事件，但所有共同签名均超过保护性污染门槛；
5. `no_shared_new_representation`：没有新增签名同时覆盖三个主导事件。

只有第2类允许建议下一轮独立策略实验；第3—5类均不得产生交易规则。

## 8. 固定产物

- `artifacts/protocol.json`；
- `artifacts/identity_audit.json`；
- `artifacts/descriptor_definitions.csv`；
- `artifacts/event_new_representation.csv`；
- `artifacts/discovered_signatures.csv`；
- `artifacts/confirmed_signatures.csv`；
- `artifacts/permutation_audit.json`；
- `artifacts/representation_classification.json`；
- `artifacts/metrics.json`。

本轮不生成图形、订单、候选策略、冻结策略或holdout结果。正式执行只运行一次；支持或否定假设均须原样归档。
