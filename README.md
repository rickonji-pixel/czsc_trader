# CZSC Trader

本仓库提供行情准备、冻结基线回测、预注册研究、隔离复现和Git研究档案验证。跨设备研究边界与当前588080正式基线见 [RESEARCH_HANDOFF.md](docs/RESEARCH_HANDOFF.md)。

唯一用户入口是安装后生成的 `czsc-trader` 命令。旧`scripts`脚本和runner模块CLI已经删除，不存在第二套调用方式。

## 当前状态

- 活动基线：`baseline_20260826`，来源为`0824_EX04`冻结的12因子四层策略；
- 588080行情：30分钟、日线和周线均覆盖至2026-08-28并通过校验；
- 最新正式研究档案：`0901_EX18`，当前共有43个Git跟踪实验档案；
- CZSC研究结论：0901_EX05—EX14从4,747个规范因子中确认一个历史风险缓解事件，形成`0901_EX13`历史挑战者；其2026仓位路径与活动基线相同，尚无晋升证据；
- 仓位研究结论：已测试的入场定仓、动态仓位、下行风险、三类风险压力和EX13计分分档均未产生可晋升仓位方案，活动基线不变；
- 证据边界：截至2026-08-28的行情均已被观察，新的未观察前向行情从该日期之后开始。

上述状态是文档快照；实际基线身份只认`configs/rule_baselines/registry.json`，行情范围只认当前manifest与校验结果，研究事实只认Git实验档案。

## 环境

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows -e ".[test]"
.\.venv\Scripts\python.exe -m pip check
~~~

行情适配器是独立子项目 [packages/dataflows/README.md](packages/dataflows/README.md)，
与主项目通过上面的同一条命令安装。若需从Tushare准备行情，将根目录
`.env.example`复制为被Git忽略的`.env`并填写令牌。`dataflows`包不依赖
`czsc_trader`，未来可整体拆分为独立仓库。

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

默认 stdout 是UTF-8编码的单一JSON文档，进度和诊断进入stderr；可用 --format text 查看人工可读结果。所有命令都支持 --repo-root，未指定时从当前目录向上发现仓库。

## 数据准备与验证

~~~powershell
.\.venv\Scripts\czsc-trader.exe data prepare --symbol 600519.SH --asset stock --start 2024-01-01 --end 2026-08-28
.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
~~~

A股股票与ETF统一使用Tushare后复权行情。只有证券基础信息、复权因子、30分钟交易时段、每日8根K线及三频对账全部通过后才发布到 data/raw；普通回测不会隐式联网刷新数据。每个`*_manifest.json`顶层保存Tushare返回的`name`中文简称，`data validate`会校验并返回该字段。

## 冻结基线回测

查看并验证基线：

~~~powershell
.\.venv\Scripts\czsc-trader.exe baseline list
.\.venv\Scripts\czsc-trader.exe baseline validate --version baseline_20260826 --symbol 588080.SH
~~~

执行588080当前活动基线：

~~~powershell
.\.venv\Scripts\czsc-trader.exe backtest run --symbol 588080.SH --asset etf --windows configs\backtest_windows\2026.json --window 2026FULL
~~~

窗口配置中的`2026FULL`左右边界分别从已验证日线动态解析为2026年第一个和最后一个有记录的交易日，不需要在行情更新后手工修改日期。普通回测只比较冻结策略与Buy & Hold的收益率、夏普率和最大回撤率及三项差值，不执行目标判定。

未显式指定基线时，所有标的默认使用`baseline_20260826`，即冻结的`0824_EX04`四层策略。该规则允许跨标的普通回测，但其调参与研究证据仍只来自588080，其他标的结果不自动构成外推有效性证据。`baseline_20260823`只允许显式历史复现。普通回测只应用冻结规则，不搜索或修改参数；输出进入`outputs/<证券代码>_<MMDD>_BTXX`，实际目录以命令返回的`artifacts.output_dir`为准。

## 研究与档案

新实验必须先创建并预注册新的 `MMDD_EXX` 目录：

~~~powershell
.\.venv\Scripts\czsc-trader.exe experiment run --dir experiments\MMDD_EXX
~~~

存在有效 experiment_manifest.json 的冻结目录会被拒绝原地执行。需要历史复现时必须指定源目录之外、尚不存在的隔离输出：

~~~powershell
$replayDir = Join-Path ([System.IO.Path]::GetTempPath()) `
  ("czsc-trader-0824_EX08-" + [guid]::NewGuid())
.\.venv\Scripts\czsc-trader.exe experiment replay `
  --dir experiments\0824_EX08 --output $replayDir
~~~

验证一个或全部Git研究档案：

~~~powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all
~~~

研究事实、当前边界和正式研究流程以 [RESEARCH_HANDOFF.md](docs/RESEARCH_HANDOFF.md)
为准。Git跟踪的实验档案是研究事实；`outputs/`和隔离回放目录都不是研究证据。

最近完成的CZSC终局计划（`0901_EX05`—`0901_EX14`）在冻结信息空间内从4,747个规范因子压缩出一个稳定风险缓解事件，并形成`0901_EX13`历史挑战者；`0901_EX15`显示其2026计分虽不同，但仓位路径和业绩与活动基线完全一致。随后`0901_EX16`—`0901_EX18`终止了不具备跨年单调证据的计分分档仓位路线。上述结论只覆盖冻结的信号、参数、周期、因子变换、线性计分与检验协议，不等于整个CZSC因子库或所有权重架构已经穷尽。

## 测试

测试集包括统一CLI端到端验证，以及仓位状态机、风险特征、资金账本、因果事件和技术错误重试的针对性单元与回归测试。

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests
~~~
完整跨设备接力流程以研究交接文档为准。
