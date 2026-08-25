# CZSC Trader Research

本仓库提供行情准备、冻结基线回测、预注册研究、隔离复现和Git研究档案验证。跨设备研究边界与当前588080正式基线见 [RESEARCH_HANDOFF.md](docs/RESEARCH_HANDOFF.md)。

唯一用户入口是安装后生成的 czsc-trader 命令。旧 scripts 脚本和 runner 模块CLI已经删除，不存在第二套调用方式。

## 环境

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip check
~~~

若需从Tushare准备行情，再安装可选数据依赖并配置被Git忽略的 dataflows/.env：

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r dataflows\requirements-dataflows.txt
~~~

## 统一命令

~~~text
czsc-trader data prepare
czsc-trader data validate
czsc-trader baseline list
czsc-trader baseline show
czsc-trader baseline validate
czsc-trader backtest run
czsc-trader experiment run
czsc-trader experiment replay
czsc-trader archive validate
~~~

默认 stdout 是单一JSON文档，进度和诊断进入stderr；可用 --format text 查看人工可读结果。所有命令都支持 --repo-root，未指定时从当前目录向上发现仓库。

## 数据准备与验证

~~~powershell
.\.venv\Scripts\czsc-trader.exe data prepare --symbol 600519.SH --asset stock --start 2024-01-01 --end 2026-08-24
.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
~~~

A股股票与ETF统一使用Tushare后复权行情。只有复权因子、30分钟交易时段、每日8根K线及三频对账全部通过后才发布到 data/raw；普通回测不会隐式联网刷新数据。

## 冻结基线回测

查看并验证基线：

~~~powershell
.\.venv\Scripts\czsc-trader.exe baseline list
.\.venv\Scripts\czsc-trader.exe baseline validate --version baseline_20260826 --symbol 588080.SH
~~~

执行588080当前活动基线：

~~~powershell
.\.venv\Scripts\czsc-trader.exe backtest run --symbol 588080.SH --asset etf --targets configs\backtest_targets\588080_2026.json
~~~

未显式指定基线时，588080默认使用 baseline_20260826，即冻结的0824EX04四层策略。baseline_20260823只允许显式历史复现。普通回测只应用冻结规则，不搜索或修改参数；输出进入 outputs/<证券代码>_<MMDD>_RXX，实际目录以命令返回的 artifacts.output_dir 为准。

## 研究与档案

新实验必须先创建并预注册新的 MMDD_EXX 目录：

~~~powershell
.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments\<新实验>
~~~

存在有效 experiment_manifest.json 的冻结目录会被拒绝原地执行。需要历史复现时必须指定源目录之外、尚不存在的隔离输出：

~~~powershell
.\.venv\Scripts\czsc-trader.exe experiment replay --dir experiments\<冻结实验> --output replays\<冻结实验>
~~~

验证一个或全部Git研究档案：

~~~powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all
~~~

研究事实只认Git跟踪的 experiments 档案；outputs 和 replay目录不是研究证据。2026行情已在项目层面观察，不能重新作为独立留出样本。

## 测试

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests
~~~
完整跨设备接力流程以研究交接文档为准。
