"""Tushare ETF market-data adapter."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .bar_utils import (
    adjustment_factor_sha256,
    apply_hfq_adjustment,
    drop_incomplete_intraday_bar,
    infer_asset_type,
    normalize_adjustment_factors,
    normalize_period,
    standardize_vendor_ohlcv,
    validate_a_share_30m_bars,
)
from .formatting import format_dataframe_report
from .market_resolver import MARKET_A_SHARE, detect_market, normalize_symbol_for_vendor
from .tushare_common import get_tushare_pro


_TUSHARE_ETF_VOLUME_X100_DATES = {
    "515050.SH": {
        "2024-04-03",
        "2024-04-19",
        "2024-04-26",
        "2024-04-30",
        "2024-05-24",
        "2024-05-31",
        "2024-06-14",
    },
    "588080.SH": {
        "2024-04-03",
        "2024-04-19",
        "2024-04-26",
        "2024-04-30",
        "2024-05-24",
        "2024-05-31",
        "2024-06-14",
    },
}


def _intraday_boundary(value: str, *, end: bool) -> str:
    if " " in value:
        return value
    return f"{value} {'23:59:59' if end else '00:00:00'}"


def _calendar_year_segments(start_date: str, end_date: str) -> list[tuple[str, str]]:
    """Split long minute requests to stay below Tushare's silent row cap."""
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    segments: list[tuple[str, str]] = []
    for year in range(start.year, end.year + 1):
        segment_start = max(start, pd.Timestamp(year=year, month=1, day=1))
        segment_end = min(end, pd.Timestamp(year=year, month=12, day=31))
        segments.append(
            (segment_start.date().isoformat(), segment_end.date().isoformat())
        )
    return segments


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


def _apply_known_intraday_volume_corrections(
    dataframe: pd.DataFrame, ts_code: str
) -> tuple[pd.DataFrame, list[str]]:
    """Correct explicitly confirmed Tushare ETF intraday volume unit errors."""
    frame = dataframe.copy()
    correction_dates = _TUSHARE_ETF_VOLUME_X100_DATES.get(ts_code)
    if correction_dates is None or frame.empty:
        return frame, []
    trade_dates = pd.to_datetime(frame["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    mask = trade_dates.isin(correction_dates)
    frame.loc[mask, "Volume"] = frame.loc[mask, "Volume"] / 100.0
    corrected_dates = sorted(trade_dates.loc[mask].dropna().unique().tolist())
    return frame, corrected_dates


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
    env_file: str | Path | None = None,
) -> tuple[pd.DataFrame, str, str]:
    period = normalize_period(period)
    market = detect_market(symbol)
    if market != MARKET_A_SHARE:
        raise ValueError("tushare_etf only supports A-share ETFs")

    ts_code = normalize_symbol_for_vendor(symbol, "tushare", market)
    if infer_asset_type(ts_code, "auto") != "fund":
        raise ValueError(f"{symbol} is not recognized as an A-share ETF")

    pro = get_tushare_pro(env_file)
    if period == "30m":
        pieces = [
            pro.etf_mins(
                ts_code=ts_code,
                start_date=_intraday_boundary(segment_start, end=False),
                end_date=_intraday_boundary(segment_end, end=True),
                freq="30min",
            )
            for segment_start, segment_end in _calendar_year_segments(
                start_date, end_date
            )
        ]
        dataframe = pd.concat(
            [piece for piece in pieces if piece is not None and not piece.empty],
            ignore_index=True,
        ) if any(piece is not None and not piece.empty for piece in pieces) else pd.DataFrame()
    else:
        dataframe = pro.fund_daily(
            ts_code=ts_code,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
        )

    if dataframe is None or dataframe.empty:
        return pd.DataFrame(), market, ts_code

    normalized = _standardize_etf_ohlcv(dataframe, intraday=period == "30m")
    corrected_dates: list[str] = []
    if period == "weekly":
        normalized = _resample_weekly(normalized)
    if period == "30m":
        normalized, corrected_dates = _apply_known_intraday_volume_corrections(
            normalized, ts_code
        )
        normalized = _merge_opening_auction_into_first_30m_bar(normalized)
        normalized = drop_incomplete_intraday_bar(normalized)
        validate_a_share_30m_bars(normalized)
    if corrected_dates:
        normalized.attrs["hardcoded_volume_corrections"] = corrected_dates
    return normalized, market, ts_code


def _fetch_hfq_factors(
    ts_code: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
) -> pd.DataFrame:
    dataframe = get_tushare_pro(env_file).fund_adj(
        ts_code=ts_code,
        start_date=start_date.replace("-", ""),
        end_date=end_date.replace("-", ""),
    )
    return normalize_adjustment_factors(dataframe)


def fetch_etf_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str,
    period: str = "daily",
    *,
    env_file: str | Path | None = None,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Return normalized Tushare ETF bars and machine-readable metadata."""
    normalized_period = normalize_period(period)
    fetch_period = "daily" if normalized_period == "weekly" else normalized_period
    dataframe, market, ts_code = _fetch_tushare_etf_ohlcv(
        symbol,
        start_date,
        end_date,
        period=fetch_period,
        env_file=env_file,
    )
    if dataframe.empty:
        raise ValueError(f"Tushare returned no data for {symbol} {normalized_period}")
    correction_dates = dataframe.attrs.get("hardcoded_volume_corrections", [])
    factors = _fetch_hfq_factors(
        ts_code, start_date, end_date, env_file=env_file
    )
    dataframe = apply_hfq_adjustment(dataframe, factors)
    if normalized_period == "weekly":
        dataframe = _resample_weekly(dataframe)
    metadata = {
        "vendor": "tushare",
        "market": market,
        "vendor_symbol": ts_code,
        "period": normalized_period,
        "asset_type": "etf",
        "adjustment": "hfq",
        "adjustment_factor_source": "fund_adj",
        "adjustment_factor_sha256": adjustment_factor_sha256(factors),
    }
    if correction_dates:
        metadata["hardcoded_volume_corrections"] = ",".join(correction_dates)
    return dataframe.copy(), metadata


def get_etf(
    symbol: str,
    start_date: str,
    end_date: str,
    period: str = "daily",
) -> str:
    try:
        dataframe, metadata = fetch_etf_ohlcv(symbol, start_date, end_date, period)
        return format_dataframe_report(
            f"Tushare ETF data for {symbol}",
            dataframe,
            {
                "Vendor": "tushare",
                "Market": metadata["market"],
                "Vendor symbol": metadata["vendor_symbol"],
                "Start date": start_date,
                "End date": end_date,
                "Period": normalize_period(period),
                "Asset type": "fund",
                "Adjustment": "hfq",
            },
            max_rows=10000,
        )
    except Exception as exc:
        return f"Error retrieving ETF data for {symbol} via tushare: {exc}"
