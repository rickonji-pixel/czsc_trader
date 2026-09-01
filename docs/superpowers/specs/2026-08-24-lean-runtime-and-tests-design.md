# 精简功能入口与测试集设计

**日期：** 2026-08-24  
**状态：** 待用户审阅  
**适用项目：** `czsc_trader`

## 1. 目标

项目不建立fast、integration、research等测试分层，而是删除已经失效或重复的研究能力，只保留当前正式工作流所需的功能入口、代码路径和测试用例。

唯一测试命令保持为：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

目标是在不改变行情、冻结基线、回测行为、实验规则和历史结论的前提下，将默认测试耗时从约61秒降至20秒以内。

## 2. 当前问题与根因

当前默认套件有109项测试，耗时约61秒。其中两个未标记为slow的月度walk-forward测试分别耗时约15秒和30秒，占总耗时约75%。它们会在360个交易日上按月反复遍历旧候选集合，并在因果性测试中重复运行完整流程。

另外两个slow测试执行旧的23,760候选搜索。其中完整研究测试还会重复选优，并生成因子、审计、CSV和三张大型HTML。slow套件运行超过5分钟仍未完成。旧研究入口使用2026目标窗口进行样本内选优，与当前“2020—2025研究、2026严格不可见”的冠军—挑战者协议冲突。

因此根因不是pytest缺少更多标记，而是仓库同时保留了两代研究体系，并用全量研究执行充当代码测试。

## 3. 唯一保留的功能入口

项目只保留以下四个用户入口：

1. `scripts/prepare_market_data.py`：获取、后复权、验证并发布行情。
2. `scripts/run_backtest.py`：加载冻结基线执行固定规则回测，写入 `outputs/`。
3. `scripts/run_experiment.py`：执行预注册的样本内实验，写入 `experiments/MMDD_EXX/`。
4. `scripts/run_holdout.py`：只对已冻结挑战者执行不可见样本外测试，写入新的实验档案。

入口职责不可交叉：回测不搜索参数；实验不写入 `outputs/`；留出集入口不重新搜索或修改挑战者。

## 4. 删除的旧研究路径

删除以下入口和实现：

- `scripts/run_research.py`；
- `src/czsc_trader/research.py::run_research`；
- `run_dated_research`；
- `_render_report` 及仅服务于旧研究输出的编排代码；
- 旧23,760候选常量 `CANDIDATES`；
- `select_fixed_rule`；
- `rank_candidate_results`；
- `deduplicate_candidate_targets`；
- 月度walk-forward候选、选择器和结果类型；
- `run_walk_forward` 及其价格模拟、规则距离和月度重选辅助函数。

历史研究事实继续由Git历史、冻结基线和正式实验档案保存。删除可执行旧代码不改变历史结论。

## 5. 保留并重命名的规则核心

现有 `walk_forward.py` 的名称和职责已经不准确。新建 `src/czsc_trader/rules.py`，只保留生产路径实际使用的固定规则原语：

- `FACTOR_COLUMNS`；
- `Rule`；
- `AppliedRule`；
- `positions_for_rule`；
- `build_factor_events`；
- `apply_fixed_rule`。

更新以下模块的导入：

- `baselines.py`；
- `backtest_runner.py`；
- `experiments.py`；
- 对应测试。

完成后删除 `walk_forward.py`，项目中不得残留该模块的运行时导入。

## 6. 输出目录工具

普通回测仍需要 `create_output_dir`。将它迁移到 `src/czsc_trader/output_paths.py`，保持签名和行为不变：

```python
create_output_dir(outputs_root: Path, symbol: str, run_date: date) -> Path
```

`backtest_runner.py` 改从新模块导入。删除旧 `research.py` 后，固定回测的 `outputs/<证券代码>_<MMDD>_RXX` 行为必须完全一致。

## 7. 精简后的测试范围

保留一套默认pytest测试，不定义slow或network标记。

### 7.1 固定规则

保留最小但完整的状态机测试：

- 连续入场确认；
- 最短持仓期；
- 连续离场确认及恢复重置；
- 入场门控；
- 仓位转换与因子事件一一对应；
- `apply_fixed_rule` 返回仓位、分数和事件。

这些测试使用数个交易日的内存数据，不运行候选搜索。

### 7.2 数据

保留：

- Tushare接口mock契约；
- ETF长区间分钟数据按年分段；
- 后复权方向；
- 30分钟、日线、周线对账；
- 行情哈希和validation；
- 研究cutoff在打开文件前排除2026。

不执行实时联网测试。

### 7.3 固定回测

保留：

- 最新/显式基线解析；
- 次日开盘执行；
- 成本和净值手算对账；
- 周期资金独立；
- 订单事件溯源；
- 输出目录及必需产物；
- 普通回测不产生候选搜索文件。

### 7.4 实验与档案

保留：

- 再入场冷却和门控语义；
- 严格收益率且夏普率PASS；
- 所有窗口必须通过；
- 20个预注册候选；
- 实验编号按日期重置；
- 四份研究文档和文件哈希；
- 文件篡改、遗漏和本机outputs溯源拒绝；
- 研究入口、留出集入口与普通回测输出边界。

### 7.5 图表

保留能覆盖用户明确要求的关键回归：

- 主副图联动悬停；
- 信息面板位于虚线左侧；
- 因子入场红色、离场绿色；
- 因子信号、成交和K线使用不同视觉通道；
- 春节、五一等非交易日折叠。

共享一次模块级行情和因子夹具，避免每个图表测试重复读取和生成相同输入。只保留一个离线HTML写入测试。

## 8. 删除的测试

删除：

- `tests/test_research.py`；
- 23,760候选数量、去重、排名和完整选择测试；
- 两个月度walk-forward全流程测试；
- `select_fixed_rule`全量候选测试；
- pyproject中的slow/network标记和默认排除表达式。

现有 `tests/test_walk_forward.py` 重命名为 `tests/test_rules.py`，只保留固定规则原语测试。

正式研究不再由pytest重跑。研究正确性由以下证据共同保证：预注册协议、固定规则单元测试、实验编排测试、完整候选CSV、行情哈希和实验档案清单。

## 9. 文档调整

README和 `docs/RESEARCH_HANDOFF.md` 只描述四个保留入口。删除旧 `run_research.py`、23,760候选和slow/network测试命令。

历史设计和实施计划可以保留旧术语作为历史记录，但当前操作说明不得引导用户执行已删除入口。

## 10. 验收标准

1. `rg`确认运行时代码、当前README和交接文档不再引用 `run_research`、`run_walk_forward`、`select_fixed_rule`、23,760候选或 `czsc_trader.research`。
2. 四个保留入口均可导入并显示帮助或通过mock入口测试。
3. 冻结基线规则文件、注册表和便携验证快照无差异。
4. `experiments/0824_EX01` 的文件和哈希保持不变。
5. `pytest -q` 全部通过，不包含deselected测试。
6. 同一设备连续运行两次，测试耗时均不超过20秒。
7. `compileall`、`pip check` 和 `git diff --check` 通过。

## 11. 非目标

- 不重新研究或调整冠军；
- 不缩小正式实验候选空间；
- 不读取2026留出集；
- 不改变回测收益、夏普率或交易结果；
- 不引入pytest并行、缓存、超时或额外插件；
- 不迁移或删除新规范建立前的其他本机历史目录。
