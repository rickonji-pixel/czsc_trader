# Financial dataflows

This package provides text-report adapters and machine-readable DataFrame APIs
for Tushare market data, indicators, fundamentals, and news.

## Setup

Install the optional runtime dependencies into the project environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r dataflows\requirements-dataflows.txt
```

Copy `dataflows/.env.example` to `dataflows/.env`, then fill the local token:

```dotenv
TUSHARE_TOKEN=your-token
```

The process environment takes precedence over `dataflows/.env`. The real
`dataflows/.env` file is ignored by Git.

## Stock and ETF bars

The adapters accept `period="daily"`, `period="weekly"`, or `period="30m"`.
Tushare A-share ETFs use the dedicated `tushare_etf` adapter; do not pass ETF
symbols such as `588080.SH` to `tushare_stock`.

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

stock_frame, stock_metadata = fetch_stock_ohlcv(
    "600519.SH", "2026-01-01", "2026-08-14", period="daily"
)
etf_frame, etf_metadata = fetch_etf_ohlcv(
    "588080.SH", "2026-01-01", "2026-08-14", period="daily"
)
```

The `fetch_*_ohlcv` functions raise vendor and empty-data errors directly and
are the stable machine interface used by `scripts/prepare_market_data.py`.

A-share stock and ETF bars are backward-adjusted (`hfq`) by default during
fetching. Stock factors come from `adj_factor` and ETF factors from `fund_adj`.
OHLC is multiplied by the trade-date factor, volume is divided by it, and
actual turnover amount is unchanged. Weekly bars are aggregated only after
daily adjustment. Returned metadata records the adjustment mode, factor
source, and a stable SHA-256 of the complete requested factor series.

Tushare returns 100x intraday volume for `588080.SH` on seven confirmed 2024
trade dates (`04-03`, `04-19`, `04-26`, `04-30`, `05-24`, `05-31`, `06-14`).
Only this symbol/date allowlist is divided by 100, and the corrected dates are
recorded in fetch metadata. No heuristic correction is applied elsewhere.

The normalized columns are `Date`, `Open`, `High`, `Low`, `Close`, `Volume`,
and `Amount`. A-share 30-minute bars are checked for duplicate timestamps,
valid OHLCV relationships, and valid session close times. A future-labeled
current bar is removed until its close time.

The Tushare ETF adapter uses `fund_daily` for daily bars, applies `fund_adj`,
aggregates adjusted daily bars into weekly bars, and uses `etf_mins` for
30-minute bars. Tushare's
09:30 opening-auction record is folded into the 10:00 bar.

For a strict historical-data gate, reconcile a complete 30-minute frame with
an independently fetched daily frame. This catches overlapping cumulative
bars even when every day still contains eight timestamps:

```python
from dataflows.bar_utils import validate_30m_against_daily

validate_30m_against_daily(intraday_dataframe, daily_dataframe)
```

Tushare endpoints may require product-specific permissions or points.
Tushare Doc URL：https://tushare.pro/document/2?doc_id=290
