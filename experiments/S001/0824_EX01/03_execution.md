# 执行过程

## 执行身份

- 实验编号：`0824_EX01`
- 执行日期：2026-08-24（Asia/Shanghai）
- Git分支：`research/pre2026-champion-challenge`
- 行情补全提交：`4a0e3ff`
- 研究实现提交：`49182fa`
- 说明：正式实验在上述改动提交前的同一工作树中运行，提交后的代码与运行代码一致。

## 环境

- Python：3.12.10
- CZSC：1.0.1
- vectorbt：1.1.0
- pandas：3.0.5
- NumPy：2.5.2
- Plotly：6.9.0
- 调整方式：Tushare `fund_adj` 后复权
- 行情manifest SHA-256：`aa45b09ee04ff13c5e4d75dbd38d816412e5ade66ac155f9e59a5474325a04ce`

## 实际命令

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_market_data `
  --symbol 588080.SH --asset etf `
  --start 2020-01-01 --end 2026-08-21

.\.venv\Scripts\python.exe scripts\run_experiment.py
```

## 数据获取异常与处理

首次长区间获取失败，表现为30分钟数据只从2022-12-21开始，而日线从上市日2020-11-16开始，缺失511个交易日。排查确认Tushare `etf_mins` 长请求发生静默行数截断，数据源中对应年度数据实际存在。

修复方式是按自然年分段请求30分钟数据，再合并、排序、去重并执行30分钟/日线、日线/周线交叉验证。修复后：

- 30分钟K线：11,200根；
- 完整交易日：1,400日；
- 周线周期：295个；
- 数据验证状态：PASS。

## 研究执行事实

- 声明候选：20个；
- 实际生成候选：20个；
- 最佳候选：`cooldown0_gate-trend`；
- 最佳候选通过年度数：1；
- 通过0个年度的候选：16个；
- 通过1个年度的候选：4个；
- 通过2或3个年度的候选：0个；
- `frozen_challenger.json`：未生成；
- 2026留出集访问：否；
- 可见行情哈希：18个，均为2020—2025年度文件；
- 2026行情哈希：0个。

## 验证

- 聚焦测试：28 passed；
- 完整快速离线测试：95 passed，2 deselected；
- `compileall`：通过；
- `pip check`：`No broken requirements found`；
- 基线规则和注册表差异：无；
- `git diff --check`：通过。

所有候选、冠军指标和运行汇总原样保存在 [artifacts](artifacts/) 中。
