# S002 研究交接

## 当前身份

- 名称：中证500择时策略；
- 标的：`510500.SH`；
- 研究线阶段：`RESEARCH`；SM身份已登记，尚无策略版本和部署资格；
- 研究数据：`2019-10-08`至`2026-09-08`；
- 当前开发截止：`2026-09-08`；
- 最近实验：`experiments/20260909_S002_EX02`。

## 当前结论

510500专属CZSC信号普查已完成。CZSC 1.0.1注册表的222个K线函数按30分钟、日线、
周线形成666个默认配置，其中662个成功生成；4个缺失项来自两个日线专用函数在30分钟和
周线无输出。正式观察区间识别2058个主状态和128,862个状态切换事件，并完成1、3、5、
10、20日因果效果、逐年稳定性及精确行为重复审计。

本轮没有生成S002候选。S001十二因子只作为普通参照标签；S001-v2也不再承担参数种子
角色。当前证据只支持形成510500专属信号假设，不能解释为策略有效性证明。

## 下一步

创建`20260909_S002_EX03`，先做信号假设设计，暂不进入权重或参数搜索：

1. 从EX02的正向与负向复核清单出发，检查3、5、10日效果方向是否一致；
2. 对照`redundancy_map.json`去除行为重复项，并在图上抽查信号出现位置和CZSC语义；
3. 只保留少量逻辑互补的入场、退出、过滤信号，写清每个信号预期解决的问题；
4. 预注册首批稀疏、可解释的策略原型后，才调用TDR做组合回测和SE评估。

若语义检查与跨期限证据不能形成有经济解释的组合，继续保持研究状态，不创建S002版本。

开始前验证：

```powershell
.\.venv\Scripts\czsc-trader.exe data validate --symbol 510500.SH
.\.venv\Scripts\czsc-trader.exe strategy show --strategy S002
.\.venv\Scripts\python.exe -c "from pathlib import Path; from czsc_trader.experiment_archive import validate_experiment_archive; print(validate_experiment_archive(Path('experiments/20260909_S002_EX02'))['status'])"
```
