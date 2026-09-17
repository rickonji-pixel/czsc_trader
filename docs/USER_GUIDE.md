# 用户使用说明

> 本文是CZSC Trader与CZSC PTE的统一用户手册，覆盖安装、数据、策略、回测、决策、
> 模拟交易和日常运维。策略研究流程见[策略研究交接](../research/README.md)，系统架构与开发
> 规则见[技术交接](DEVELOPMENT_HANDOFF.md)。以下命令均在仓库根目录执行。

## 环境与安装

- Python 3.12；
- 发布新行情时需要Tushare Token，将`.env.example`复制为`.env`并填写
  `TUSHARE_TOKEN`；
- 使用Futu模拟交易时需要启动Futu OpenD；
- 只有安装、更新或删除WDG系统服务时需要Windows管理员权限。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows
.\.venv\Scripts\python.exe -m pip install -e ".\packages\factor_signal_catalog[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_template_catalog[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_manager[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\strategy_evaluator[test]"
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip install -e ".\packages\paper_trading_engine[test]"
```

确认命令入口：

```powershell
.\.venv\Scripts\czsc-trader.exe --help
.\.venv\Scripts\pte.exe --help
Get-Command .\.venv\Scripts\pte-watchdog.exe
```

## 因子与信号目录

FSC用于查看项目已经登记的研究“弹药”。目录状态只表示定义是否可复用，不代表存在Alpha：

```powershell
.\.venv\Scripts\czsc-trader.exe catalog validate
.\.venv\Scripts\czsc-trader.exe catalog list --kind factor --status READY
.\.venv\Scripts\czsc-trader.exe catalog list --kind signal --family MARKET_STRUCTURE
.\.venv\Scripts\czsc-trader.exe catalog show --id F-PROJECT-ER60
```

标的计算值、筛选结果和收益证据不在FSC中查看，应进入对应的`research/<策略ID>/`与
`experiments/<策略ID>/`。

## 策略模板目录

STC用于选择受控的策略函数结构。模板状态表示结构契约可复用，不代表策略有效：

```powershell
.\.venv\Scripts\czsc-trader.exe template validate
.\.venv\Scripts\czsc-trader.exe template list --status READY
.\.venv\Scripts\czsc-trader.exe template show --id STC-T04-EVENT-HOLD
.\.venv\Scripts\czsc-trader.exe template instantiate --spec .\prototype.json
```

`instantiate`只校验输入角色和参数并生成确定性实例ID。策略收益、排名与冻结仍需经过实验、
Optuna搜索、SE评估和SM治理。

## 行情数据

### 研究数据集

`data/raw/`是受控研究池。准备或验证研究数据：

```powershell
.\.venv\Scripts\czsc-trader.exe data prepare `
  --symbol 588080.SH --asset etf `
  --start 2020-01-01 --end 2026-09-02

.\.venv\Scripts\czsc-trader.exe data validate --symbol 588080.SH
```

研究必须在实验明确指定的数据边界内进行。查看研究数据、改变候选或选择策略版本时，
遵守[策略研究交接](../research/README.md)中的数据污染规则。

### 普通回测数据集

`data/backtest/`独立维护，回测命令不会隐式更新数据：

```powershell
.\.venv\Scripts\czsc-trader.exe data update-backtest `
  --symbol 588080.SH --asset etf `
  --strategy S001 --strategy-version v2 `
  --through 2026-09-04
```

更新采用追加保护：既有30分钟线、日线和已结束周线不可改写；只有与新增日线处于同一
自然周的末根未完成周线可以重新聚合。校验或网络失败时保留原数据集。

发布时必须指定冻结策略版本。发布器据策略的数据契约准备通用的30分钟、日线、周线、
未复权执行价格及身份清单；若策略依赖5分钟或1分钟数据，也会一起准备、校验和原子发布。
策略统一使用Tushare后复权行情；交易委托、成交和估值使用未复权行情。

## 策略管理

当前正式策略包括`S001`与`S002`。查看策略及证据：

```powershell
.\.venv\Scripts\czsc-trader.exe strategy list
.\.venv\Scripts\czsc-trader.exe strategy show --strategy S001 --version v1
.\.venv\Scripts\czsc-trader.exe strategy history --strategy S001
.\.venv\Scripts\czsc-trader.exe strategy performance --strategy S001 --version v1
.\.venv\Scripts\czsc-trader.exe strategy validate --all
```

`RESEARCH`表示版本仍可修改；`PAPER_READY`可以创建模拟账户；人工晋升到`LIVE_READY`
后才具备实盘部署资格；`RETIRED`禁止创建新运行实例。资格变化不会直接启停PTE。

旧命名基线只作为不可变历史依赖查看：

```powershell
.\.venv\Scripts\czsc-trader.exe baseline list
.\.venv\Scripts\czsc-trader.exe baseline show `
  --version baseline_20260903 --symbol 588080.SH
.\.venv\Scripts\czsc-trader.exe baseline validate `
  --version baseline_20260903 --symbol 588080.SH
```

`baseline_20260903`是`S001-v1`的只读历史别名。新策略版本统一登记在`strategies/`。
候选评估、冠军确认和冻结操作见[策略研究交接](../research/README.md)。

## 回测

先显式更新普通回测数据集，再执行确定性回测：

```powershell
.\.venv\Scripts\czsc-trader.exe backtest run `
  --strategy S001 --strategy-version v1 --dataset backtest `
  --symbol 588080.SH --asset etf `
  --start 2026-01-05 --end 2026-09-04 --init-cash 100000
```

Backtest v2每次回放一个不可变策略快照、一个标的、一个明确日期区间。账户从指定现金和
零持仓开始；T日完整收盘决策在T+1执行。买入开盘价不高于限价时按开盘成交，盘中最低
价严格低于限价时按限价成交，相等触价保持未成交；卖出按下一交易日开盘成交。

`--symbol`可以指定与策略参考标的不同、但资产类型相同的实际回测标的，用于跨标的泛化
测试。该绑定只对本次回测生效，不修改策略注册、SM部署范围、PTE账户或`advice`行为。
报告和`manifest.json`会同时记录策略参考标的、实际回测标的及应用方式。

每次回测同时以独立的同额资金账户计算BuyHold和MA5/MA20参照，统一比较最大回撤、
卡玛比率、盈亏比、收益率和夏普率。参照策略不参与当前策略的SE审计或通过判定。

结果写入被Git忽略的`outputs/`，包括报告、账本、审计证据和交互图表。正式研究证据必须
归档到`experiments/策略ID/YYYYMMDD_策略ID_EXnn/`；历史实验ID继续有效。验证全部实验档案：

```powershell
.\.venv\Scripts\czsc-trader.exe archive validate --all
```

## 交易决策链路

冻结策略不再提供人工`advice run`入口。PTE在每个发布周期读取虚拟账户绑定的冻结版本，
由SRT声明并通过DFLS发布全部输入，随后在进程内计算决策和执行计划。数据历史不足、截止日
未达到、发布代际混合、源码闭包与冻结绑定不一致时，链路明确失败并阻止下单。

普通调仓使用`advice.v4`契约；需要多个执行时点或成交依赖的策略使用`advice.v5`原子计划。
只有Futu明确返回的累计成交增量可以改变实际持仓；决策生成和委托受理均不等于成交。

## 新闻筛选与事件抽取

TDR可以调用单一MaaS模型，对已经缓存的新闻逐篇完成相关性判断和结构化事件抽取。当前
固定使用用户创建的DeepSeek-V4.1-Flash服务；模型只生产研究事件，不直接生成交易信号
或修改策略。

运行前把模型端点和API Key写入仓库根目录`.env`。该文件已被Git忽略，凭证不会进入
命令参数、输出文件或版本库；临时覆盖某个值时可设置同名进程环境变量，进程环境变量优先：

```dotenv
CZSC_NEWS_MAAS_API_KEY=<API Key>
CZSC_NEWS_MAAS_MODEL=ep-dsv41flash
```

`CZSC_NEWS_MAAS_MODEL`直接填写控制台显示的`ep-*`服务ID。默认通过腾讯云境内地址
`https://tokenhub.tencentmaas.com/v1/chat/completions`调用；如控制台明确提供其他地域或
套餐地址，再用`CZSC_NEWS_MAAS_API_URL`覆盖。`CZSC_NEWS_MAAS_ENDPOINT`已废弃，TDR
不会读取该变量。

输入为CSV或CSV.GZ，至少包含`sample_id,pub_time,src,title,content`；如包含
`raw_sha256`，TDR会同时验证原文身份。HTML原文保留用于身份哈希，调用模型和证据回查
统一使用去除脚本、样式和标签后的可见正文。执行S005开发池的首次小批量验证：

```powershell
.\.venv\Scripts\czsc-trader.exe news extract `
  --input .tmp\research_cache\S005\news\ex39_sina_full_pilot.csv.gz `
  --scope research\S005\news_scope.v1.json `
  --output-dir .tmp\news_events\S005-ex39-v41 `
  --limit 10 `
  --workers 4
```

每篇文章单独保存请求、MaaS原始响应、结构化结果、模型、提示词版本、原文哈希和请求ID。
当前自定义服务使用`json_object`响应格式，字段、枚举、事件完整性和原文证据由TDR按冻结
Schema再次严格校验。若模型生成的文章级相关性证据不是连续原文、但事件证据均已通过回查，
TDR会确定性地使用第一条事件证据作为文章级证据；模型原始响应仍完整保留。
重复执行时只复用身份完全一致且已经通过校验的文章。最终生成`reviews.jsonl`、核心披露
`events.jsonl`和`manifest.json`；一篇汇总稿可以生成多项本次新增事件，历史背景不得进入事件表。
跨文章去重键只是“可能重复”的候选分组，不自动删除事件。只要任一文章调用失败、JSON不合法、相关篇没有事件，或
证据无法在原文中定位，整批命令明确返回`FAIL`，已成功记录可以在下次执行时继续复用。

新闻事件能被可靠抽取只说明数据生产链路可用。将事件映射为收益假设前，仍须按研究交接
文档预注册机制、信息可用时间、竞争解释和评价口径。

## PTE模拟交易

PTE以虚拟账户为业务中心。每个虚拟账户绑定一个不可变策略发布和一个Futu渠道；一个
Futu渠道可以承载多个虚拟账户。渠道只负责执行、回报和对账，不绑定策略或生成决策。

运行前启动Futu OpenD，并确认存在唯一中国市场模拟账户。观测与干预页面为
<http://127.0.0.1:8080>。

默认运行规则：

- 订单和成交约每5秒对账，账户与持仓每60秒刷新；
- 每个交易日20:30后发布完整收盘数据，失败后退避重试；
- 需要成分股等附加信息的策略会同时发布自己的运行时支持数据；只有市场数据与支持数据
  到达同一截止日后才生成决策；
- 发布成功后，每个运行账户只生成一次对应数据版本的决策；
- 当日发布和全局决策完成后新建的账户，会自动补发该策略的附加数据并单独生成首次决策；
- 新单只在有效交易日`09:30–11:30`、`13:00–14:57`提交；
- 暂停只阻止新订单，已有订单继续对账；撤单必须二次确认；
- 只有Futu明确返回的累计成交增量能够改变账户现金和持仓。

虚拟账户页的“订单意图”展示决策到Futu订单之间的状态。`待提交/提交中/已提交`属于正常
流转；“提交结果待确认”表示通信中断后无法确认券商是否受理，PTE会保留冻结资金、阻塞
该账户并同时查询Futu当前和历史订单。明确拒单、过期、撤单和部分成交后撤单会释放未成交
冻结资金，并形成必须人工复核的“前瞻执行缺口”。账户在复核完成前不会自动恢复下单。
模拟盘买入使用DAY限价单；普通策略目标清仓按冻结规则使用可成交DAY限价单，日内计划可
按冻结计划使用市价卖单。同一决策明确拒单后，只能在
有效交易日内以递增执行序号重试；旧意图永久保留为终态审计证据。
控制台全局告警会汇总渠道阻塞、虚拟账户阻塞、待确认提交和数据发布失败。

日内计划会在“订单意图”中分别显示计划环节和计划时点。依赖环节显示“等待前序成交及
计划时点”；只有前序订单达到计划要求的明确状态才会转为待提交。以当前核心仓位轮换为例，
开盘买入必须全部成交，11:30卖出才可放行；买入未完整成交时PTE会撤单并阻断卖出，禁止
用旧库存伪造一次完整轮换。计划窗口同时绑定有效交易日和北京时间；未来交易日的计划不会
因为当前时刻已经晚于同名时刻而提前过期。进程重启不会丢失上述依赖关系。

策略输入由PTE按活跃账户统一发布，不再维护“行情发布＋策略附加数据”的两套命令。单个输入
失败时查看控制台“审计事件”和系统告警；禁止用旧的`prepare-strategy-support`命令拼补数据。

处理“前瞻执行缺口”时，先在Futu确认订单、成交和持仓，再在对应虚拟账户页点击
“复核后确认”，填写可审计的处理结论。PTE会先重新执行一次订单、持仓和账本对账；对账
通过且该账户没有其他待确认事项时才恢复账户。禁止通过直接修改SQLite或简单点击恢复运行
绕过该流程。

### 虚拟账户

首次启动会幂等创建`s001-v1 / S001-v1模拟账户`，绑定`S001-v1`并分配10万元初始
资金。查看、创建、暂停和恢复账户：

```powershell
.\.venv\Scripts\pte.exe account list --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte.exe account create --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-v2 --name "S001-v2模拟账户" `
  --strategy S001 --strategy-version v2
.\.venv\Scripts\pte.exe account create --repo-root D:\CodeBase\czsc_trader `
  --account-id s002-v1 --name "S002-v1模拟账户" `
  --strategy S002 --strategy-version v1 `
  --symbol 510500.SH --asset etf --initial-cash 100000
.\.venv\Scripts\pte.exe account pause `
  --repo-root D:\CodeBase\czsc_trader --account-id s001-v2
.\.venv\Scripts\pte.exe account resume `
  --repo-root D:\CodeBase\czsc_trader --account-id s001-v2
```

本地活动订单在Futu当前及历史订单中缺失、无法归属的订单、订单关键字段不一致，或Futu
持仓与虚拟账户汇总持仓不一致时，渠道会阻止新单。
控制台的审计事件页可以按账户、策略、渠道和关联ID查询策略、交易、系统及其他事件。
页面统一显示北京时间。

### 前瞻观察与绩效

虚拟账户页的“前瞻观察”展示行情、CZSC笔、策略得分、信号、成交、目标持仓和实际持仓。
默认保留策略选择截止日前最后60个交易日作为背景，截止线右侧才属于该策略版本的前瞻
记录。查看图表不会改变数据身份；若使用新行情调参，新版本必须登记新的选择截止日。

登记模拟盘里程碑时，先由PTE导出证据，再由Trader写入策略注册表：

```powershell
.\.venv\Scripts\pte.exe performance export --repo-root D:\CodeBase\czsc_trader `
  --account-id s001-v2 --recorded-by tomxiao `
  --start 2026-09-03 --end 2026-12-03 --output state\paper-forward.json
.\.venv\Scripts\czsc-trader.exe strategy evidence add --input state\paper-forward.json
```

日常净值保存在SQLite；只有人工复核、晋升或降级所需的里程碑证据进入Git。

## PTE与WDG启停

正式运行由WDG系统服务托管PTE。首次安装时，在管理员PowerShell中执行：

```powershell
.\.venv\Scripts\pte-watchdog.exe install-config --repo-root D:\CodeBase\czsc_trader
.\.venv\Scripts\pte-watchdog.exe start --wait 30
```

系统服务名为`CZSC-PTE-Watchdog`，启动类型为自动。WDG每10秒检查PTE进程、8080
HTTP状态及调度器心跳；连续3次失败后按5、30、60秒退避重启。心跳只用于识别进程内部
调度停滞，交易结果、OpenD可用性和数据发布成败仍由PTE告警与退避机制处理。

日常发布PTE代码或修改PTE业务配置后，只重启PTE，无需管理员权限：

```powershell
.\.venv\Scripts\pte.exe control restart `
  --repo-root D:\CodeBase\czsc_trader --wait 30
Invoke-RestMethod http://127.0.0.1:8080/api/status
```

PTE完成当前请求并正常退出，WDG随即拉起新实例。只有WDG自身升级、仓库路径或监听地址
变化时，才在管理员PowerShell中维护系统服务：

```powershell
Get-Service CZSC-PTE-Watchdog
Start-Service CZSC-PTE-Watchdog       # 同时启动PTE
Stop-Service CZSC-PTE-Watchdog        # 同时停止PTE
Restart-Service CZSC-PTE-Watchdog     # 同时重启WDG和PTE
.\.venv\Scripts\pte-watchdog.exe remove
```

开发调试时可直接运行`.\.venv\Scripts\pte.exe serve --repo-root .`，并通过`Ctrl+C`
结束。生产环境由WDG托管时不要再启动第二个`serve`实例，也不要并发执行`pte once`。
PTE会对`runtime.db`持有操作系统级独占锁，第二个写进程会明确失败，防止重复下单和账本
并发写入。

## 状态、日志与故障检查

```powershell
Get-Service CZSC-PTE-Watchdog
Get-NetTCPConnection -LocalPort 8080 -ErrorAction SilentlyContinue
Invoke-RestMethod http://127.0.0.1:8080/api/status
Get-Content state\paper_trading\logs\watchdog.log -Tail 100
Get-Content state\paper_trading\logs\pte.log -Tail 100
```

端口冲突、数据库已被其他PTE写进程占用都会明确报错。恢复运行失败时先确认Futu OpenD
可用，再检查渠道的账户、订单、成交和持仓对账。调度器心跳停滞会显示全局告警并触发
WDG重启；数据发布告警结合交易日20:30后的审计事件和日志判断。

PTE每次由`serve`启动前会在`state\paper_trading\backups\`创建一致性SQLite备份，默认
滚动保留3份；PTE日志达到10 MiB后滚动，默认保留5份。备份用于故障恢复，恢复前仍须
与Futu订单、成交和持仓逐笔核对，不能仅凭数据库备份继续下单。

若告警明确为终态买入意图已经解冻、但该冻结代次缺少`INTENT_RELEASE`账本记录，使用
运行中PTE的受保护修复入口，禁止直接修改SQLite：

```powershell
.\.venv\Scripts\pte.exe control repair-ledger `
  --repo-root D:\CodeBase\czsc_trader `
  --account-id s003-v1 --intent-id PTE-XXXXXXXXXXXXXXXXXXXX
```

该命令先刷新Futu账户快照，只在渠道持仓与逻辑持仓一致、意图为需要人工复核的终态买单、
没有渠道订单、且差额恰好等于该代冻结金额时追加补偿账本。成功返回`REPAIRED`；重复执行
返回`ALREADY_REPAIRED`；任何证据不完整的情况均明确失败。修复后再刷新渠道对账并人工
确认执行缺口，原拒单和审计事件继续保留，不能补单或改写为正常样本。

本机数据库、发布数据、图表缓存和日志位于`state/paper_trading/`且不进入Git。跨机延续
同一条模拟盘观察序列时，需要迁移完整运行目录，并重新核对Futu活动订单、成交和持仓。
