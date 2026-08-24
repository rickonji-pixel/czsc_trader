# 研究结论

## 判定边界

本轮是诊断实验，不判定策略PASS/FAIL，不更新冠军，也不产生挑战者。以下结论只描述当前冠军在2021—2025可见样本中的反事实归因。

## 核心结论

1. 因子、底层信号和语义映射中没有对象满足“稳定负向”定义，因此没有证据支持直接删除某个现有因子或映射状态；
2. `trend`聚合因子是稳定正向因素：移除后2021—2024的收益率和夏普率均下降，仅2025改善；
3. 日线成交量状态 `高量N9`是稳定正向因素：将其贡献置零后，2022—2025的收益率和夏普率均下降，仅2021改善；
4. 33项因素表现为状态依赖，说明当前主要问题不是某个因子在所有年份都错误，而是同一信号在不同市场状态下作用反转；
5. 唯一达到4/5年度稳定局部改善要求的规则变化，是将入场确认从1个交易日改为2个交易日。它识别了当前即时入场规则是首要优化对象，但2023的作用方向反转，因此不能把固定2日确认直接视为挑战规则，也没有形成新冠军。

### 入场确认2日相对冠军

| 年度 | 收益率增量 | 夏普率增量 | 年度方向 |
|---|---:|---:|---|
| 2021 | +20.9861个百分点 | +1.4435 | 改善 |
| 2022 | +6.2054个百分点 | +0.4389 | 改善 |
| 2023 | -4.4724个百分点 | -0.4411 | 恶化 |
| 2024 | +14.8897个百分点 | +0.4267 | 改善 |
| 2025 | +2.3508个百分点 | +0.1283 | 改善 |

各年度最大单一持仓差异区间占比均低于50%，上述4/5年度改善不受预注册的单一区间主导规则否决。但固定2日确认在2023同时降低收益率和夏普率，按“每个验收窗口均优于冠军”的PASS规则已知不能通过。所以下一轮应把“入场确认机制”作为重点优化对象，先解释并约束2023的反向作用，再冻结一个能够同时覆盖不同市场状态的单一挑战规则；不应直接重复测试固定2日确认，也没有证据支持优先删除因子、改变权重或调整阈值。

## 因素分类

### 稳定正向（2项）

| 类型 | 对象 | 反事实 | 收益中位增量 | 夏普中位增量 |
|---|---|---|---:|---:|
| group | `trend` | `zero_contribution` | -13.1871% | -0.5225 |
| state | `raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5::高量N9` | `zero_mapped_state` | -7.1547% | -0.2848 |

### 状态依赖（33项）

| 类型 | 对象 | 反事实 | 收益中位增量 | 夏普中位增量 |
|---|---|---|---:|---:|
| group | `structure` | `zero_contribution` | +0.7256% | +0.0596 |
| group | `volume_position` | `zero_contribution` | +3.1406% | +0.1021 |
| signal | `raw__30m__cxt_bi_status_V230101` | `drop_and_reaggregate` | +3.2525% | +0.1843 |
| signal | `raw__30m__cxt_bi_status_V230101` | `zero_contribution` | +1.5343% | +0.1201 |
| signal | `raw__daily__cxt_bi_status_V230101` | `drop_and_reaggregate` | +3.1364% | +0.1641 |
| signal | `raw__daily__cxt_bi_status_V230101` | `zero_contribution` | -2.0434% | -0.0726 |
| signal | `raw__daily__cxt_five_bi_V230619__di_1` | `drop_and_reaggregate` | +5.2556% | +0.2941 |
| signal | `raw__daily__cxt_five_bi_V230619__di_1` | `zero_contribution` | +0.0000% | +0.0000 |
| signal | `raw__daily__cxt_seven_bi_V230620__di_1` | `zero_contribution` | -0.6105% | -0.0453 |
| signal | `raw__daily__pressure_support_V240406__di_1__w_20` | `drop_and_reaggregate` | -11.8027% | -0.4551 |
| signal | `raw__daily__pressure_support_V240406__di_1__w_20` | `zero_contribution` | +0.7976% | +0.0237 |
| signal | `raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_10` | `drop_and_reaggregate` | +0.0750% | +0.0007 |
| signal | `raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_10` | `zero_contribution` | -3.6095% | -0.1538 |
| signal | `raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_20` | `drop_and_reaggregate` | +0.9018% | +0.0268 |
| signal | `raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_20` | `zero_contribution` | -5.9930% | -0.1849 |
| signal | `raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_5` | `drop_and_reaggregate` | +4.2110% | +0.3080 |
| signal | `raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_5` | `zero_contribution` | -0.5703% | -0.0342 |
| signal | `raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5` | `drop_and_reaggregate` | -3.1588% | -0.4095 |
| signal | `raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5` | `zero_contribution` | +5.1933% | +0.1676 |
| signal | `raw__weekly__cxt_bi_status_V230101` | `drop_and_reaggregate` | +3.8238% | +0.2697 |
| signal | `raw__weekly__cxt_bi_status_V230101` | `zero_contribution` | +1.3169% | +0.1084 |
| state | `raw__30m__cxt_bi_status_V230101::向上` | `zero_mapped_state` | -0.5702% | -0.0406 |
| state | `raw__30m__cxt_bi_status_V230101::向下` | `zero_mapped_state` | -0.6955% | -0.0208 |
| state | `raw__daily__cxt_bi_status_V230101::向下` | `zero_mapped_state` | -0.4064% | -0.0307 |
| state | `raw__daily__cxt_five_bi_V230619__di_1::aAb式底背驰` | `zero_mapped_state` | +0.0000% | +0.0000 |
| state | `raw__daily__pressure_support_V240406__di_1__w_20::支撑位` | `zero_mapped_state` | +0.5891% | +0.0177 |
| state | `raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_10::多头` | `zero_mapped_state` | +1.0948% | +0.0867 |
| state | `raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_20::多头` | `zero_mapped_state` | -0.6197% | -0.0191 |
| state | `raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_5::多头` | `zero_mapped_state` | +1.7561% | +0.1330 |
| state | `raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_5::空头` | `zero_mapped_state` | -1.6797% | -0.1235 |
| state | `raw__daily__tas_macd_base_V221028__di_1__fastperiod_12__signalperiod_9__slowperiod_26::多头` | `zero_mapped_state` | +0.2869% | +0.0178 |
| state | `raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5::高量N10` | `zero_mapped_state` | +3.1900% | +0.1057 |
| state | `raw__weekly__cxt_bi_status_V230101::向下` | `zero_mapped_state` | +0.9585% | +0.0226 |

### 冗余（3项）

`raw__30m__cxt_third_buy_V230228__di_1`、`raw__30m__cxt_third_buy_V230228__di_1::三买`、`raw__daily__cxt_five_bi_V230619__di_1::类三买`

### 证据不足（14项）

`raw__30m__cxt_third_buy_V230228__di_1`、`raw__daily__cxt_seven_bi_V230620__di_1`、`raw__daily__tas_macd_base_V221028__di_1__fastperiod_12__signalperiod_9__slowperiod_26`、`raw__daily__tas_macd_base_V221028__di_1__fastperiod_12__signalperiod_9__slowperiod_26`、`raw__daily__cxt_bi_status_V230101::向上`、`raw__daily__cxt_seven_bi_V230620__di_1::向上中枢完成`、`raw__daily__cxt_seven_bi_V230620__di_1::向下中枢完成`、`raw__daily__pressure_support_V240406__di_1__w_20::压力位`、`raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_10::空头`、`raw__daily__tas_ma_base_V221101__di_1__ma_type_SMA__timeperiod_20::空头`、`raw__daily__tas_macd_base_V221028__di_1__fastperiod_12__signalperiod_9__slowperiod_26::空头`、`raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5::高量N7`、`raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5::高量N8`、`raw__weekly__cxt_bi_status_V230101::向上`

### 样本不足（15项）

`raw__daily__cxt_bi_status_V230101::其他`、`raw__daily__cxt_five_bi_V230619__di_1::aAb式顶背驰`、`raw__daily__cxt_five_bi_V230619__di_1::类趋势底背驰`、`raw__daily__cxt_five_bi_V230619__di_1::类趋势顶背驰`、`raw__daily__cxt_seven_bi_V230620__di_1::aAbcd式底背驰`、`raw__daily__cxt_seven_bi_V230620__di_1::aAbcd式顶背驰`、`raw__daily__cxt_seven_bi_V230620__di_1::aAb式底背驰`、`raw__daily__cxt_seven_bi_V230620__di_1::aAb式顶背驰`、`raw__daily__cxt_seven_bi_V230620__di_1::abcAd式底背驰`、`raw__daily__cxt_seven_bi_V230620__di_1::abcAd式顶背驰`、`raw__daily__cxt_seven_bi_V230620__di_1::类三买`、`raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5::其他`、`raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5::高量N2`、`raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5::高量N3`、`raw__weekly__cxt_bi_status_V230101::其他`

### 未建模状态（11项）

`raw__30m__cxt_third_buy_V230228__di_1::其他`、`raw__daily__cxt_five_bi_V230619__di_1::上颈线突破`、`raw__daily__cxt_five_bi_V230619__di_1::下颈线突破`、`raw__daily__cxt_five_bi_V230619__di_1::其他`、`raw__daily__cxt_five_bi_V230619__di_1::类三卖`、`raw__daily__cxt_seven_bi_V230620__di_1::其他`、`raw__daily__cxt_seven_bi_V230620__di_1::类三卖`、`raw__daily__pressure_support_V240406__di_1__w_20::其他`、`raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5::高量N4`、`raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5::高量N5`、`raw__daily__vol_window_V230731__di_1__m_30__n_10__w_5::高量N6`

## 局部敏感性

- 权重：未发现稳定局部改善方向。
- 阈值：未发现稳定局部改善方向。
- 状态机：`entry_confirm_days / confirm_days=2`。

## 后续边界

只有归类为稳定负向的对象可以进入下一轮优化候选清单；证据不足、状态依赖或单一区间主导的对象不得直接优化。2026保持不可见，冠军继续为 `baseline_20260823`。
