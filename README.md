# 588080 CZSC 多因子滚动策略

本项目只读取 `data/raw` 中的 588080.SH K线，通过 CZSC 1.0.1 生成多周期因子，按月使用历史数据滚动选择参数，并由 vectorbt 1.1.0 在下一交易日开盘执行和验证。

## 运行

```powershell
.\.venv\Scripts\python.exe scripts\run_research.py
```

结果写入 `outputs/latest`，其中 `report.md` 汇总2026Q1、2026H1和2026年1–8月相对 Buy & Hold 的收益。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试覆盖原始数据完整性、CZSC因子截断不变性、滚动参数因果性、vectorbt次日开盘成交、成本手算对账和最终审计产物。
