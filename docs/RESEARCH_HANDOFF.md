# 588080 纯 CZSC 多因子策略：研究总结与会话交接

## 1. 当前有效结论

当前唯一应作为正式结果使用的目录是：

```text
D:\CodeBase\czsc_trader\outputs\588080_0823_R05
```

R05 使用一个全历史固定不变的纯 CZSC 因子规则。最终仓位只能由 CZSC 因子状态机产生，不包含收益、Buy & Hold、季度日期或组合净值触发的仓位覆盖。

| 周期 | 策略收益 | Buy & Hold | 超额收益 | 最大回撤 | 订单数 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2026Q1 | 10.41% | -7.07% | +17.47% | -3.81% | 4 | PASS |
| 2026H1 | 82.16% | 64.39% | +17.78% | -7.74% | 7 | PASS |
| 2026年1—8月 | 56.90% | 22.37% | +34.53% | -14.48% | 14 | PASS |

R05 审计状态为 `PASS`：检查 25 笔周期订单、16 个被订单引用的唯一因子事件和 639 个每日目标仓位。三个周期是独立组合，因此同一个全局因子事件可能分别被多个周期引用。`factor_events.csv` 共保存 63 个全历史因子事件，包括周期初始仓位对齐事件。

### 结果性质

用户明确允许根据 2026 年已经发生的结果调参。最终规则从 144 个预先声明的固定候选中，按三个目标周期的表现选择。因此：

- 每一天的因子和成交满足无前视与 T+1 开盘执行；
- 规则不在历史中途切换，也没有针对特定日期的交易分支；
- 但最终绩效属于 **2026 样本内优化结果**，不能表述为样本外验证或未来收益保证。

## 2. 必须遵守的研究目标和硬约束

### 研究目标

以 `588080.SH` 为唯一标的，分别验证：

- 2026Q1；
- 2026H1；
- 2026年1月到数据末日 2026-08-21。

目标是每个周期的策略净收益高于同周期 Buy & Hold。若约束内无法达到目标，必须如实报告 FAIL，禁止通过例外规则强行达标。

### 硬约束

1. 只能读取 `data/raw` 中的 30 分钟、日线和周线历史 K 线。
2. 所有仓位决策必须直接来自 CZSC 1.0.1 输出的结构或信号。
3. 因子日 `T` 只能读取不晚于 `T` 收盘的已完成数据，最早在下一交易日开盘成交。
4. 交易最小粒度为日，只做多或空仓，目标仓位只能是 `0` 或 `1`。
5. 三个周期分别以 1,000,000 元现金、零持仓启动；首日可以执行上一交易日已经形成且仍有效的 CZSC 因子状态。
6. 正式回测必须由 vectorbt 1.1.0 完成，并用独立现金/持仓账本复算。
7. 每笔订单必须有 `factor_event_id`，能够追溯到具体因子快照和仓位变化。
8. 禁止收益锁定、基准表现触发、季度强制持仓、特殊日期交易或任何非因子仓位覆盖。
9. 允许查看 2026 结果后调整 CZSC 信号、权重、阈值、确认天数和最短持仓，但必须标注样本内优化。
10. 开发只能在主仓库的本地功能分支进行，禁止使用 Git worktree。

更完整的有效规范见：

- `docs/superpowers/specs/2026-08-23-czsc-only-remediation-design.md`
- `docs/superpowers/plans/2026-08-23-czsc-only-remediation.md`

## 3. 已废弃结果与违规原因

### R03 不得用于验收

R03 增加了“Q1策略跑赢 Buy & Hold 后，从Q1末强制满仓至年底”的覆盖规则。该规则虽然在3月31日收盘后判断、4月1日开盘执行，没有交易时点前视，但它不是 CZSC 因子，因此违反因子来源硬约束。

具体异常表现是：

```text
2026-03-31 base_target_position = 0
2026-03-31 overlay target_position = 1
2026-04-01 策略买入，但没有对应因子入场
```

该机制及相关 API、测试、README、manifest 和输出已经从正式代码删除。R05 中不存在4月1日买入；4月的下一笔买入由4月9日因子信号触发，并于4月10日开盘成交。

### R04 的定位

R04 已使用纯 CZSC 固定规则，绩效与 R05 相同，但它生成于最后一次“T+1必须是紧邻下一交易日”审计加固之前。最终交付统一使用由最终 `master` 代码重新生成的 R05。

## 4. 最终策略如何计算

### 4.1 CZSC 原始信号

结构因子输入：

- 30分钟 `cxt_bi_status_V230101`；
- 30分钟 `cxt_third_buy_V230228`；
- 日线 `cxt_bi_status_V230101`；
- 周线 `cxt_bi_status_V230101`；
- 日线 `cxt_five_bi_V230619`；
- 日线 `cxt_seven_bi_V230620`。

趋势因子输入：

- 日线5、10、20日 `tas_ma_base_V221101`；
- 日线 `tas_macd_base_V221028`。

量价位置因子输入：

- 日线 `vol_window_V230731`；
- 日线 `pressure_support_V240406`。

### 4.2 信号映射

原始 CZSC 类别先映射为 `-1/0/+1`：

- 向上、多头、三买、底背驰、支撑位：`+1`；
- 向下、空头、顶背驰、压力位：`-1`；
- 其他、中性、未识别类别：`0`；
- 成交量排名 N7—N10：`+1`，N1—N3：`-1`，N4—N6：`0`。

每组内部等权平均，得到 `structure`、`trend` 和 `volume_position`，范围限制在 `[-1, 1]`。

### 4.3 最终固定规则

```text
factor_score = structure       × 0.40
             + trend           × 0.40
             + volume_position × 0.20
```

状态机参数：

| 参数 | 值 |
| --- | ---: |
| 入场阈值 | 0.20 |
| 离场阈值 | 0.10 |
| 入场连续确认 | 2天 |
| 最短持仓 | 1天 |

空仓时，综合分数连续两天不低于0.20才转为满仓；持仓时，综合分数不高于0.10且满足最短持仓天数才转为空仓。当日目标变化在下一交易日开盘执行。

### 4.4 候选选择顺序

144 个固定候选按以下顺序排序：

1. 三个目标周期通过数量；
2. 三个周期中的最小超额收益；
3. 平均超额收益；
4. 更低换手；
5. 更小最大回撤；
6. 更简单的确认和持仓参数。

完整排行榜保存在 `candidate_results.csv`，最终规则保存在 `selected_rule.json`。

## 5. 无前视与订单溯源

### 信号和成交顺序

```text
T日收盘：生成CZSC信号、三个组因子、综合分数和目标仓位
T+1交易日开盘：vectorbt按目标仓位成交
T+1收盘：按收盘价计算当日组合净值
```

审计不仅要求 `signal_date < execution_date`，还要求执行日必须是信号日之后紧邻的下一条交易日期。

### 因子事件类型

- `Entry`：目标仓位从0变为1；
- `Exit`：目标仓位从1变为0；
- `InitialEntry`：独立周期从现金启动，但上一交易日的有效因子目标已经为1，因此在周期首日开盘对齐。

普通订单必须对应同日同方向的 `Entry` 或 `Exit`；周期首日买入对应 `InitialEntry`。审计还检查订单方向、事件日期、目标仓位变化和事件综合分数。

## 6. 代码结构

| 文件 | 职责 |
| --- | --- |
| `src/czsc_trader/data.py` | 只读加载原始CSV、校验字段/时间/OHLC、跨周期对账和SHA-256 |
| `src/czsc_trader/factors.py` | 调用CZSC、日末对齐原始信号、映射分数并生成三个组因子 |
| `src/czsc_trader/walk_forward.py` | 定义固定候选、因子状态机、候选排序和因子事件；文件名保留历史名称 |
| `src/czsc_trader/backtest.py` | vectorbt次日开盘回测、独立账本、周期独立启动和订单来源绑定 |
| `src/czsc_trader/audit.py` | 审计目标仓位、订单事件匹配及紧邻下一交易日执行 |
| `src/czsc_trader/charting.py` | 生成三张离线Plotly日线交互图 |
| `src/czsc_trader/research.py` | 编排完整研究、输出文件、报告、manifest和版本信息 |
| `scripts/run_research.py` | 单一正式运行入口 |
| `tests/` | 数据、因子截断、状态机、候选选择、成交、审计、图表和研究集成测试 |

`walk_forward.py` 中还保留未被正式管线调用的早期月度滚动函数，用于历史测试和对照。正式 `run_research` 只调用 `select_fixed_rule`，不得重新接入收益覆盖逻辑。

## 7. R05 输出文件说明

| 文件 | 含义 |
| --- | --- |
| `report.md` | 三周期结果、独立指标、审计摘要、规则和局限 |
| `metrics.json` | 机器可读的三周期指标、整体PASS和审计数据 |
| `manifest.json` | 数据截止日、原始数据哈希、固定规则、依赖版本和图表清单 |
| `selected_rule.json` | 最终固定规则参数 |
| `candidate_results.csv` | 144个候选的完整排序及三周期表现 |
| `factors.csv` | 每日目标仓位、综合分数、阈值、三个组因子和全部原始CZSC信号 |
| `factor_events.csv` | 全历史入场、离场及周期初始对齐事件 |
| `orders_<周期>.csv` | vectorbt实际订单，含信号日、成交日、价格、费用和事件ID |
| `equity_<周期>.csv` | 每日收盘、目标/执行仓位、综合分数和组合净值 |
| `chart_<周期>.html` | 独立离线交互图，含日K、CZSC笔、背驰、因子事件和成交点 |

图表不绘制线段。五笔/七笔背驰在目标周期没有满足条件时不会显示标记，但相应检测层仍存在。

## 8. 环境、运行和验证

仓库：

```text
D:\CodeBase\czsc_trader
```

CZSC源码和示例：

```text
D:\CodeBase\czsc
```

核心版本：

- Python 3.12；
- CZSC 1.0.1；
- vectorbt 1.1.0；
- pandas 3.0.5；
- numpy 2.5.2；
- Plotly 6.9.0。

安装或更新本项目：

```powershell
cd D:\CodeBase\czsc_trader
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip check
```

完整测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
git diff --check
```

当前基线是29项测试全部通过。

生成新结果：

```powershell
.\.venv\Scripts\python.exe scripts\run_research.py
```

输出目录自动使用 `outputs/588080_0823_RXX` 的下一个未占用版本，不覆盖历史结果。

## 9. Git状态和开发流程

纯 CZSC 合规重构的实现基线提交：

```text
6cd1c617407c3d0dd2067883fe915343c67e284a
```

该提交已经合并到 `master` 并通过SSH推送。远端配置为：

```text
fetch: https://github.com/tomxiao/czsc_trader.git
push:  git@github.com:tomxiao/czsc_trader.git
```

HTTPS fetch 曾发生过连接重置；需要核对远端时可以直接使用：

```powershell
git ls-remote git@github.com:tomxiao/czsc_trader.git refs/heads/master
```

后续开发流程：

```powershell
git switch master
git status --short
git switch -c research/<任务名称>
```

所有编辑、测试和回测都在主仓库的本地分支执行。禁止运行 `git worktree add`，禁止在 `.worktrees` 下开发。仓库中可能仍存在历史 `.worktrees` 目录，不使用，也不要未经确认删除。

完成后在功能分支跑全量验证，再快进合并到 `master`，最后通过SSH推送并比较本地/远端SHA。

## 10. 已知局限与风险

1. 仅有一只ETF和约2.6年数据，无法证明跨标的、跨市场或跨周期稳健性。
2. 规则由2026目标周期结果选择，存在样本内过拟合风险。
3. 当前候选空间只有144个低自由度规则；扩大空间可能提高样本收益，也会显著提高过拟合风险。
4. 未识别CZSC类别共84次，统一按中性0分处理；增加映射前必须逐类验证含义。
5. 回测允许按资金比例持有小数份额，适合研究比较，不等同于真实交易中的整数份额约束。
6. 单边成本固定为0.05%，没有单独建立随流动性变化的滑点模型。
7. R05是研究验证结果，不是投资建议或未来收益承诺。

## 11. 建议的后续工作

优先级从高到低：

1. 获取2026-08-21之后的新数据，冻结当前规则后进行真正样本外验证；新数据进入前不得再调整规则。
2. 增加整数份额、可配置滑点和成交额容量约束，并保留与当前研究口径的对照结果。
3. 对84次未知CZSC类别生成分类清单，确认语义后决定是否增加显式映射。
4. 在不查看目标结果的前提下扩展到其他ETF，检验规则迁移能力。
5. 如需重构，将 `walk_forward.py` 更名或拆分为固定规则选择模块；重构前必须保持R05订单和指标回归不变。

不得把“再次提高R05样本内收益”作为首要后续目标。

## 12. 新会话接手检查清单

新会话开始后依次执行：

1. 阅读本文件和纯CZSC合规设计文档。
2. 确认工作目录是 `D:\CodeBase\czsc_trader`，而不是任何worktree。
3. 运行 `git status --short`，保护用户已有修改。
4. 确认当前分支和 `master` 基线，不直接在 `master` 开发。
5. 阅读 R05 的 `report.md`、`manifest.json`、`selected_rule.json` 和 `factor_events.csv`。
6. 修改前先写失败测试；修改后运行29项以上完整测试。
7. 重新回测时创建新的RXX目录，不覆盖R05。
8. 检查每笔订单的 `factor_event_id`，尤其关注周期首日 `InitialEntry`。
9. 确认没有重新引入收益、基准或日期触发的仓位覆盖。
10. 结果必须明确区分样本内优化和样本外验证。

## 13. 可直接复制给新会话的提示词

```text
请接手 D:\CodeBase\czsc_trader 的 588080 纯CZSC多因子研究。

开始前完整阅读：
1. docs/RESEARCH_HANDOFF.md
2. docs/superpowers/specs/2026-08-23-czsc-only-remediation-design.md
3. outputs/588080_0823_R05/report.md
4. outputs/588080_0823_R05/manifest.json

当前有效基线是 master 上的纯CZSC固定策略，正式结果为 R05。禁止使用R03结果，禁止重新引入Q1收益锁定或任何非CZSC仓位覆盖。每笔订单必须对应factor_event_id，并在信号后的紧邻下一交易日开盘执行。

只在 D:\CodeBase\czsc_trader 主仓库创建本地功能分支开发，禁止使用git worktree。保护所有已有未提交修改。完成后运行完整测试、正式回测和订单来源审计；无论PASS或FAIL都必须如实报告。R05属于2026样本内优化，不得声称是样本外验证。
```
