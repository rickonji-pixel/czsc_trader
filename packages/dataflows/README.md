# 金融数据流（Dataflows，DFLS）

本目录是一个可以独立安装的 Python 子项目。运行时代码位于
`packages/dataflows/src/dataflows/`，且有意保持对 `czsc_trader` 的零依赖。

## 安装与配置

在仓库根目录同时安装本包和主应用：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\packages\dataflows -e ".[test]"
```

将 `.env.example` 复制为仓库根目录下的 `.env`，然后填写本地 Token：

```dotenv
TUSHARE_TOKEN=your-token
```

进程环境变量的优先级高于显式指定的凭据文件。应用层负责传入已被 Git 忽略的仓库
根目录 `.env`；作为可复用包，DFLS 不假定调用方采用特定的仓库目录结构。

## 稳定的数据发布契约

策略运行时应依赖 DFLS 门面，而不是依赖特定数据供应商的函数。每次请求都会返回
明确状态；非 `READY` 结果绝不会暴露可能被误认为发布成功的数据。

```python
from dataflows import Dataflows, DataRequest, DataStatus, Dataset

result = Dataflows().fetch(
    DataRequest(
        dataset=Dataset.ETF_OHLCV,
        symbol="588080.SH",
        start="2026-01-01",
        end="2026-09-15",
        required_cutoff="2026-09-15",
        frequency="daily",
        options={"env_file": ".env"},
    )
)
if result.status is DataStatus.READY:
    bars = result.dataframe
    identity = result.identity
else:
    error = result.error
```

公开状态包括 `READY`、`WAITING_SOURCE`、`EMPTY`、`INCOMPLETE` 和 `FAILED`。
发布成功时，数据身份会包含实际数据边界和稳定的内容哈希。DFLS 门面会拒绝时间戳
乱序、重复、无效或超出请求边界的数据。现有供应商适配器继续保留以兼容旧代码，
新的策略运行时代码应统一使用该门面。

初始数据集注册表覆盖当前所有冻结策略（`S001-v1`、`S001-v2`、`S002-v1`、
`S003-v1` 和 `S007-v1`）的原始数据依赖：

- 股票和 ETF 的复权 OHLCV，以及用于执行定价的不复权日线；
- SHIBOR、境内指数日频基本指标、全球指数日收益和 ETF 份额；
- 时点一致的指数成分权重和个股资金流；
- 交易所交易日历。

DFLS 只发布规范化的源数据及其身份。因果滞后、滚动特征、CZSC 信号、策略得分和
决策属于策略运行时职责。调用方必须在 `DataRequest` 中显式设置
`required_cutoff`；只有明确接受快照或截至当时语义时才可以传入 `None`。当数据源
没有达到声明的截止时间时，DFLS 返回 `INCOMPLETE`，不能返回 `READY`。

## 股票与 ETF 行情

A 股适配器支持 `period="daily"`、`period="weekly"`、`period="30m"`、
`period="15m"`、`period="5m"` 和 `period="1m"`。Tushare A 股 ETF 使用专用的
`tushare_etf` 适配器；不得把 `588080.SH` 等 ETF 代码传给 `tushare_stock`。

```python
from dataflows.tushare_stock import get_stock as get_tushare_stock
from dataflows.tushare_etf import get_etf as get_tushare_etf
from dataflows.tushare_stock import fetch_stock_ohlcv
from dataflows.tushare_etf import fetch_etf_ohlcv

weekly = get_tushare_stock(
    "600519.SH", "2026-01-01", "2026-08-14", period="weekly"
)
intraday = get_tushare_stock(
    "600519.SH", "2026-08-10", "2026-08-14", period="30m"
)
weekly_tushare = get_tushare_etf(
    "588080.SH", "2026-01-01", "2026-08-14", period="weekly"
)
intraday_tushare = get_tushare_etf(
    "588080.SH", "2026-08-10", "2026-08-14", period="30m"
)
decision_bars = get_tushare_etf(
    "510500.SH", "2026-08-10", "2026-08-14", period="15m"
)

stock_frame, stock_metadata = fetch_stock_ohlcv(
    "600519.SH", "2026-01-01", "2026-08-14", period="daily"
)
etf_frame, etf_metadata = fetch_etf_ohlcv(
    "588080.SH", "2026-01-01", "2026-08-14", period="daily"
)
```

`fetch_*_ohlcv` 函数会直接抛出供应商错误和空数据错误；它们也是
`czsc-trader data prepare` 使用的稳定机器接口。

A 股股票和 ETF 行情获取后默认执行后复权（`hfq`）。股票复权因子来自
`adj_factor`，ETF 复权因子来自 `fund_adj`。OHLC 乘以交易日复权因子，成交量除以
该因子，实际成交额保持不变。周线只在日线完成复权后聚合。返回的元数据记录复权
模式、因子来源，以及完整请求区间内复权因子序列的稳定 SHA-256。ETF 复权因子按
五个自然年分段获取，确保超过 Tushare 单次返回行数上限的长历史仍然完整。

Tushare 在已经确认的 2024 年七个交易日（`04-03`、`04-19`、`04-26`、
`04-30`、`05-24`、`05-31`、`06-14`），会把 `510500.SH`、`515050.SH` 和
`588080.SH` 的分钟成交量放大 100 倍。系统只对这些明确列入“标的＋日期”白名单的
数据除以 100，并把修正日期写入获取元数据；其他数据不采用启发式修正。

规范化字段为 `Date`、`Open`、`High`、`Low`、`Close`、`Volume` 和 `Amount`。
A 股分钟行情会检查时间戳重复、OHLCV 关系、各周期应有的收盘时刻，以及完整交易日
的 K 线数量。当前 K 线尚未结束时，会移除带有未来结束时间的当前柱。

Tushare ETF 适配器使用 `fund_daily` 获取日线、使用 `fund_adj` 复权，在复权日线
基础上聚合周线，并使用 `etf_mins` 获取分钟行情。Tushare 的 09:30 集合竞价记录会
根据周期并入第一根已完成 K 线，即 09:31、09:35、09:45 或 10:00。频率对账前会
移除人为生成的零成交量 ETF 交易日。长区间分钟数据按周期对应的自然日长度分段
获取，避免超过供应商行数上限。

如需执行严格的历史数据门禁，应使用独立获取的日线与完整 30 分钟数据进行对账。
即使每天仍有八个时间戳，该检查也能发现相邻周期内容重复的累计 K 线：

```python
from dataflows.bar_utils import validate_intraday_against_daily

validate_intraday_against_daily(intraday_dataframe, daily_dataframe, "15m")
```

部分 Tushare 接口需要特定产品权限或积分。
Tushare 文档：https://tushare.pro/document/2?doc_id=290
