# 实施计划

1. 验证EX08档案和源文件清单哈希。
2. 按预注册三套规则重排并断言Top 3完全一致。
3. 从EX08 Trial参数重建8个策略，并在2020—2025数据上验证目标仓位摘要。
4. 写入规则选择证据和冻结候选文件，计算冻结哈希。
5. 冻结完成后一次性加载2026行情与候选因子。
6. 复用同一矩阵回测8个候选与EX04，写入指标、订单和因果审计。
7. 生成执行、结论、清单和交接事实。
8. 运行最小相关测试、编译、依赖、diff与全部档案验证并提交。

正式入口使用模块命令，不新增`scripts/`入口：

```powershell
.\.venv\Scripts\python.exe -m czsc_trader.top3_holdout_runner `
  --experiment-dir experiments\0825_EX01 `
  --execution-commit <干净执行提交SHA>
```
