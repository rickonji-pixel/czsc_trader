# Financial dataflows

This package provides text-report adapters for Tushare market data,
indicators, fundamentals, and news.

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
```

The normalized columns are `Date`, `Open`, `High`, `Low`, `Close`, `Volume`,
and `Amount`. A-share 30-minute bars are checked for duplicate timestamps,
valid OHLCV relationships, and valid session close times. A future-labeled
current bar is removed until its close time.

The Tushare ETF adapter uses `fund_daily` for daily bars, aggregates those
daily bars into weekly bars, and uses `etf_mins` for 30-minute bars. Tushare's
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
