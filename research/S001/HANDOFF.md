# S001 研究交接

## 当前身份

- 名称：综合基线策略；
- 标的：`588080.SH`；
- 当前版本：`S001-v2`，资格`PAPER_READY`；
- 来源候选：R1102；来源实验：`experiments/S001/0903_EX06`；
- 选择数据截止：`2026-09-02`；
- 统计稳健性：`MIXED`，具体证据读取来源实验；
- `S001-v1`作为不可变历史版本在独立PTE虚拟账户继续观察。

`baseline_20260903`仅是S001-v1的历史依赖名称。研究、模拟盘和后续治理统一使用S001版本
身份。

## 后续研究入口

可继续研究range区间的回撤和交易质量。新实验需明确是否使用`2026-09-02`后的前瞻数据；
一旦用于调参或候选选择，新版本必须登记相应的新开发截止。候选继续挑战S001-v2，并完整
评估跨regime影响、最大回撤、卡玛比率、盈亏比和收益代价。

开始前验证：

```powershell
.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
.\.venv\Scripts\czsc-trader.exe strategy show --strategy S001 --version v2
```
