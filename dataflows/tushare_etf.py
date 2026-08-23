from __future__ import annotations

import pandas as pd

from .bar_utils import (
    drop_incomplete_intraday_bar,
    infer_asset_type,
    normalize_period,
    standardize_vendor_ohlcv,
    validate_a_share_30m_bars,
)
from .formatting import format_dataframe_report
from .market_resolver import MARKET_A_SHARE, detect_market, normalize_symbol_for_vendor
from .tushare_common import get_tushare_pro


def _intraday_boundary(value: str, *, end: bool) -> str:
    if " " in value:
        return value
    return f"{value} {'23:59:59' if end else '00:00:00'}"


def _resample_weekly(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe
    frame = dataframe.copy()
    frame["_timestamp"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["_timestamp"]).sort_values("_timestamp")
    frame["_week"] = frame["_timestamp"].dt.to_period("W-FRI")
    weekly = frame.groupby("_week", sort=True).agg(
        Date=("Date", "last"),
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
        Amount=("Amount", "sum"),
    )
    return weekly.reset_index(drop=True)


def _standardize_etf_ohlcv(
    dataframe: pd.DataFrame, *, intraday: bool
) -> pd.DataFrame:
    normalized = standardize_vendor_ohlcv(dataframe, intraday=intraday)
    if not intraday:
        normalized["Volume"] = normalized["Volume"] * 100
        normalized["Amount"] = normalized["Amount"] * 1000
    return normalized


def _merge_opening_auction_into_first_30m_bar(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe.copy()

    frame = dataframe.copy()
    timestamps = pd.to_datetime(frame["Date"], errors="coerce")
    merged_auction_indices: list[int] = []
    for auction_index in frame.index[timestamps.dt.strftime("%H:%M:%S") == "09:30:00"]:
        auction_time = timestamps.loc[auction_index]
        target_time = auction_time.normalize() + pd.Timedelta(hours=10)
        target_indices = frame.index[timestamps == target_time]
        if target_indices.empty:
            continue
        target_index = target_indices[0]
        frame.at[target_index, "Open"] = frame.at[auction_index, "Open"]
        frame.at[target_index, "High"] = max(
            frame.at[auction_index, "High"], frame.at[target_index, "High"]
        )
        frame.at[target_index, "Low"] = min(
            frame.at[auction_index, "Low"], frame.at[target_index, "Low"]
        )
        frame.at[target_index, "Volume"] += frame.at[auction_index, "Volume"]
        frame.at[target_index, "Amount"] += frame.at[auction_index, "Amount"]
        merged_auction_indices.append(auction_index)

    return (
        frame.drop(index=merged_auction_indices)
        .sort_values("Date")
        .reset_index(drop=True)
    )


def _fetch_tushare_etf_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    period: str = "daily",
) -> tuple[pd.DataFrame, str, str]:
    period = normalize_period(period)
    market = detect_market(symbol)
    if market != MARKET_A_SHARE:
        raise ValueError("tushare_etf only supports A-share ETFs")

    ts_code = normalize_symbol_for_vendor(symbol, "tushare", market)
    if infer_asset_type(ts_code, "auto") != "fund":
        raise ValueError(f"{symbol} is not recognized as an A-share ETF")

    pro = get_tushare_pro()
    if period == "30m":
        dataframe = pro.etf_mins(
            ts_code=ts_code,
            start_date=_intraday_boundary(start_date, end=False),
            end_date=_intraday_boundary(end_date, end=True),
            freq="30min",
        )
    else:
        dataframe = pro.fund_daily(
            ts_code=ts_code,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
        )

    if dataframe is None or dataframe.empty:
        return pd.DataFrame(), market, ts_code

    normalized = _standardize_etf_ohlcv(dataframe, intraday=period == "30m")
    if period == "weekly":
        normalized = _resample_weekly(normalized)
    if period == "30m":
        normalized = _merge_opening_auction_into_first_30m_bar(normalized)
        normalized = drop_incomplete_intraday_bar(normalized)
        validate_a_share_30m_bars(normalized)
    return normalized, market, ts_code


def fetch_etf_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str,
    period: str = "daily",
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Return normalized Tushare ETF bars and machine-readable metadata."""
    normalized_period = normalize_period(period)
    dataframe, market, ts_code = _fetch_tushare_etf_ohlcv(
        symbol,
        start_date,
        end_date,
        period=normalized_period,
    )
    if dataframe.empty:
        raise ValueError(f"Tushare returned no data for {symbol} {normalized_period}")
    return dataframe.copy(), {
        "vendor": "tushare",
        "market": market,
        "vendor_symbol": ts_code,
        "period": normalized_period,
        "asset_type": "etf",
    }


def get_etf(
    symbol: str,
    start_date: str,
    end_date: str,
    period: str = "daily",
) -> str:
    try:
        dataframe, market, ts_code = _fetch_tushare_etf_ohlcv(
            symbol, start_date, end_date, period=period
        )
        return format_dataframe_report(
            f"Tushare ETF data for {symbol}",
            dataframe,
            {
                "Vendor": "tushare",
                "Market": market,
                "Vendor symbol": ts_code,
                "Start date": start_date,
                "End date": end_date,
                "Period": normalize_period(period),
                "Asset type": "fund",
            },
            max_rows=10000,
        )
    except Exception as exc:
        return f"Error retrieving ETF data for {symbol} via tushare: {exc}"
