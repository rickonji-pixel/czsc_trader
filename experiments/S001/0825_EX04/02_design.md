# 研究设计

## 1. 研究单位

主研究单位固定为0825_EX03 `decision_events.csv`中全部20个`early_ex04_exit`事件：EX04在信号日从持仓转为空仓，而通用历史冠军在同日仍保持目标持仓。事件身份、窗口、得分、阈值、市场状态、阻断标签和三组加权贡献全部继承受跟踪EX03档案，不重新筛选。

正式运行必须先验证EX03实验清单，再逐项比对协议中冻结的源文件可移植SHA-256。事件数不是20、出现重复`event_id`、存在2026日期或源档案不再是`COMPLETE / holdout_accessed=false`时立即`ERROR`。

## 2. 事件级因果窗口

每个离场事件的实际执行日为信号日的下一交易日。反事实从该执行日开盘开始：实际路径按EX04离场后持有现金；反事实路径忽略本次离场并继续持有。

窗口在以下时点最早发生者终止：

1. EX04实际执行重新入场；
2. 通用历史冠军实际执行离场；
3. 当前半年最后一个交易日收盘。

若以重新入场终止，在重新入场开盘比较双方财富，实际路径计入离场和重新入场费用，继续持有路径不发生交易；若以通用基线离场终止，继续持有路径在该开盘离场并计费；若以半年末终止，按末日收盘盯市。费用固定为单边0.05%。

输出`counterfactual_log_advantage = log(继续持有终值 / 实际空仓终值)`，并保存持续交易日、终止原因、最大有利变动和最大不利变动。该值必须与逐日事件路径账本闭合，容差`1e-12`。

## 3. 结果标签

材料性门槛固定为绝对对数财富差0.005，约等于0.5个百分点且显著高于一次往返费用：

- `false_exit`：`counterfactual_log_advantage > 0.005`，继续持有明显更好；
- `protective_exit`：`counterfactual_log_advantage < -0.005`，实际离场明显更好；
- `neutral_exit`：其余情况。

标签是事后诊断结果，不得成为本轮交易输入。

## 4. 离场当日解释特征

只使用信号日已知或EX03已经冻结的字段：

- 类别轴：T-1可知的60日市场状态、EX03阻断标签、EX04离场前实际持仓年龄分箱、EX04趋势组贡献符号；
- 持仓年龄分箱：`short`为不超过5日，`medium`为6—20日，`long`为超过20日；
- 连续轴：通用基线得分、EX04得分、双方得分差、EX04离场阈值距离、持仓天数；
- 三组轴：结构、趋势、量价位置的双方加权贡献及贡献差。

不加入未来收益、未来回撤、未来市场状态或事件结果派生字段。类别轴只单独报告，不搜索交叉组合。

## 5. 上下文富集规则

对每个预注册类别轴的每个取值，报告事件数、非中性事件数、覆盖窗口数、错误/保护/中性数量、错误率、保护率、净反事实对数优势和最大单事件贡献份额。

错误离场上下文必须同时满足：

- 非中性事件不少于4个；
- 覆盖至少2个半年窗口；
- 错误离场率不低于75%；
- 净反事实对数优势为正；
- 该上下文最大单事件正向贡献不超过50%。

保护性上下文使用对称规则：保护性离场率不低于75%、净反事实优势为负，最大单事件保护贡献不超过50%。

连续特征只计算错误离场与保护性离场之间的Cliff's delta。`abs(delta) >= 0.474`为大效应；只有两类各不少于4个事件、至少两个窗口同时包含两类且窗口内中位差方向一致，才记为稳定大效应。连续特征不据此生成阈值。

## 6. 路径集中度

错误离场总损失使用所有`false_exit`事件的正向反事实对数优势。按贡献从大到小计算Top 1和Top 3份额：

- Top 1不低于35%，或Top 3不低于60%：`concentrated=true`；
- 否则为非集中。

同时按三个落后窗口与七个对照窗口分别报告错误离场、保护性离场和净反事实贡献，禁止只展示有利窗口。

## 7. 机器分类

分类顺序固定如下：

1. `insufficient_exit_evidence`：事件数不等于20、闭合失败、存在非有限值，或错误/保护事件任一少于3个；
2. `path_concentrated_exit_failure`：错误离场损失满足集中度门槛；
3. `systematically_poor_exit_signal`：材料性事件中错误离场占至少75%，错误离场覆盖至少5个窗口，且十个窗口中至少7个净反事实优势为正；
4. `context_dependent_exit_quality`：至少存在一个合格错误上下文和一个合格保护上下文，或存在稳定大效应连续特征；
5. `mixed_exit_quality`：证据完整但不满足以上分类。

分类只决定下一轮研究准入，不晋升策略。

## 8. 后续映射

- `context_dependent_exit_quality`：下一轮可围绕证据最稳定的单一上下文预注册条件化离场候选；
- `systematically_poor_exit_signal`：下一轮才可研究全局离场表达，但仍不得同轮修改入场；
- `path_concentrated_exit_failure`：先解释主导事件，不做策略优化；
- `mixed_exit_quality`或证据不足：停止直接离场优化；
- 2025H2入场问题始终是另一条独立假设，不得并入本轮或下一轮离场单变量实验。

## 9. 正式产物

- `artifacts/protocol.json`；
- `artifacts/identity_audit.json`；
- `artifacts/exit_event_counterfactuals.csv`；
- `artifacts/exit_event_path_ledger.csv`；
- `artifacts/exit_quality_by_window.csv`；
- `artifacts/exit_quality_by_context.csv`；
- `artifacts/continuous_feature_separation.csv`；
- `artifacts/exit_concentration.json`；
- `artifacts/exit_quality_classification.json`；
- `artifacts/metrics.json`。

无论`COMPLETE`或`ERROR`均如实归档。
