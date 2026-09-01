# czsc-trader

面向A股股票与ETF的CZSC策略研究、固定基线回测和数据验证工具。

## 环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e packages\dataflows
.\.venv\Scripts\python.exe -m pip install -e .[test]
```

根包依赖本仓库的 `czsc-dataflows`，行情发布时从 `.env` 读取
Tushare凭据。普通验证和回测不会隐式联网。

## 正式入口

```powershell
.\.venv\Scripts\czsc-trader.exe data prepare --help
.\.venv\Scripts\czsc-trader.exe data validate --help
.\.venv\Scripts\czsc-trader.exe baseline list --help
.\.venv\Scripts\czsc-trader.exe baseline show --help
.\.venv\Scripts\czsc-trader.exe baseline validate --help
.\.venv\Scripts\czsc-trader.exe backtest run --help
.\.venv\Scripts\czsc-trader.exe archive validate --help
```

CLI只保留四类资源：`data`、`baseline`、`backtest`、`archive`。
历史实验执行与重放已退役；`experiments/`只保存不可变研究档案。

## 数据

```powershell
.\.venv\Scripts\czsc-trader.exe data prepare `
  --symbol 588080.SH --asset etf `
  --start 2020-01-01 --end 2026-09-01

.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
```

A股股票与ETF统一使用Tushare后复权（`hfq`）行情。发布器同时生成并校验
30分钟、日线和周线文件以及manifest、validation文件。

当前Git跟踪数据：

| 标的 | 名称 | 起始日 | 截止日 |
| --- | --- | --- | --- |
| 159352.SZ | 南方中证A500ETF | 2025-01-01 | 2026-09-01 |
| 159516.SZ | 国泰中证半导体材料设备主题ETF | 2025-01-01 | 2026-09-01 |
| 515050.SH | 华夏中证5G通信主题ETF | 2024-01-02 | 2026-09-01 |
| 588080.SH | 易方达上证科创板50成份ETF | 2020-01-01 | 2026-09-01 |

## 活动基线

活动基线由 `configs/rule_baselines/registry.json` 决定。当前为
`baseline_20260901`，策略类型 `czsc_regime_weight`，来源候选143。

```powershell
.\.venv\Scripts\czsc-trader.exe baseline show `
  --version baseline_20260901 --symbol 588080.SH

.\.venv\Scripts\czsc-trader.exe baseline validate `
  --version baseline_20260901 --symbol 588080.SH
```

未显式指定基线的回测默认使用注册表中的活动基线。

## 回测

```powershell
.\.venv\Scripts\czsc-trader.exe backtest run `
  --symbol 588080.SH --asset etf `
  --start 2026-01-01 --end 2026-08-21
```

输出目录使用 `证券代码_MMDD_BTXX` 格式并写入 `outputs/`。该目录被
Git忽略，只用于普通回测结果，不作为研究交接依据。

## 实验档案

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all
```

`experiments/MMDD_EXXX/`保存每轮实验的目标、设计、执行、结论和制品。
档案可校验但不再通过统一入口重新运行。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_cli_e2e.py -q
```

项目只保留一个不联网的端到端测试文件，验证安装后的CLI、数据、活动基线、
固定回测和全部实验档案。
