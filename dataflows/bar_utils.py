from __future__ import annotations

from typing import Any

import pandas as pd


SUPPORTED_PERIODS = {"daily", "weekly", "30m"}
A_SHARE_30M_CLOSE_TIMES = {
    "10:00:00",
    "10:30:00",
    "11:00:00",
    "11:30:00",
    "13:30:00",
    "14:00:00",
    "14:30:00",
    "15:00:00",
}

_COLUMN_ALIASES = {
    "日期": "Date",
    "时间": "Date",
    "trade_date": "Date",
    "trade_time": "Date",
    "date": "Date",
    "datetime": "Date",
    "开盘": "Open",
    "open": "Open",
    "最高": "High",
    "high": "High",
    "最低": "Low",
    "low": "Low",
    "收盘": "Close",
    "close": "Close",
    "成交量": "Volume",
    "vol": "Volume",
    "volume": "Volume",
    "成交额": "Amount",
    "amount": "Amount",
}


def normalize_period(period: str) -> str:
    value = str(period).strip().lower()
    aliases = {"day": "daily", "week": "weekly", "30min": "30m"}
    value = aliases.get(value, value)
    if value not in SUPPORTED_PERIODS:
        raise ValueError(
            f"Unsupported period `{period}`. Choose from: {sorted(SUPPORTED_PERIODS)}"
        )
    return value


def infer_asset_type(symbol: str, requested: str = "auto") -> str:
    value = requested.strip().lower()
    if value not in {"auto", "stock", "fund"}:
        raise ValueError("asset_type must be one of: auto, stock, fund")
    if value != "auto":
        return value
    code = symbol.split(".", 1)[0]
    return "fund" if code.startswith(("1", "5")) else "stock"


def standardize_vendor_ohlcv(dataframe: pd.DataFrame, *, intraday: bool = False) -> pd.DataFrame:
    if dataframe is None or dataframe.empty:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume", "Amount"])

    mapping = {column: _COLUMN_ALIASES.get(str(column), str(column)) for column in dataframe.columns}
    renamed = dataframe.rename(columns=mapping).copy()
    required = ["Date", "Open", "High", "Low", "Close"]
    missing = [column for column in required if column not in renamed.columns]
    if missing:
        raise ValueError(f"Missing required OHLC columns: {missing}")
    if "Volume" not in renamed.columns:
        renamed["Volume"] = 0
    if "Amount" not in renamed.columns:
        renamed["Amount"] = 0

    timestamps = pd.to_datetime(renamed["Date"], errors="coerce")
    renamed = renamed.loc[timestamps.notna()].copy()
    timestamps = timestamps.loc[timestamps.notna()]
    renamed["Date"] = timestamps.dt.strftime(
        "%Y-%m-%d %H:%M:%S" if intraday else "%Y-%m-%d"
    )
    for column in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
        renamed[column] = pd.to_numeric(renamed[column], errors="coerce")
    return renamed[["Date", "Open", "High", "Low", "Close", "Volume", "Amount"]].sort_values(
        "Date"
    ).reset_index(drop=True)


def drop_incomplete_intraday_bar(
    dataframe: pd.DataFrame, *, now: pd.Timestamp | str | None = None
) -> pd.DataFrame:
    if dataframe is None or dataframe.empty:
        return dataframe.copy()
    cutoff = pd.Timestamp.now() if now is None else pd.Timestamp(now)
    timestamps = pd.to_datetime(dataframe["Date"], errors="coerce")
    return dataframe.loc[timestamps.notna() & (timestamps <= cutoff)].reset_index(drop=True)


def validate_a_share_30m_bars(
    dataframe: pd.DataFrame, *, require_complete_days: bool = False
) -> dict[str, Any]:
    required = {"Date", "Open", "High", "Low", "Close", "Volume"}
    missing = sorted(required.difference(dataframe.columns))
    if missing:
        raise ValueError(f"Missing required 30m columns: {missing}")

    timestamps = pd.to_datetime(dataframe["Date"], errors="coerce")
    if timestamps.isna().any():
        raise ValueError("30m data contains invalid timestamps")
    if timestamps.duplicated().any():
        raise ValueError("30m data contains duplicate timestamps")

    times = set(timestamps.dt.strftime("%H:%M:%S"))
    unexpected = sorted(times.difference(A_SHARE_30M_CLOSE_TIMES))
    if unexpected:
        raise ValueError(f"30m data contains unexpected A-share close times: {unexpected}")

    invalid_ohlc = (
        (dataframe["High"] < dataframe[["Open", "Close"]].max(axis=1))
        | (dataframe["Low"] > dataframe[["Open", "Close"]].min(axis=1))
        | (dataframe["High"] < dataframe["Low"])
        | (dataframe["Volume"] < 0)
    )
    if invalid_ohlc.any():
        raise ValueError("30m data contains invalid OHLCV relationships")

    counts = timestamps.groupby(timestamps.dt.strftime("%Y-%m-%d")).size()
    partial_days = [f"{date} has {int(count)} bars" for date, count in counts.items() if count != 8]
    if require_complete_days and partial_days:
        raise ValueError("Incomplete A-share 30m trading day: " + ", ".join(partial_days))
    return {
        "bar_count": len(dataframe),
        "complete_day_count": int((counts == 8).sum()),
        "partial_days": partial_days,
    }


def validate_30m_against_daily(
    intraday: pd.DataFrame,
    daily: pd.DataFrame,
    *,
    price_tolerance: float = 0.005,
    volume_relative_tolerance: float = 1e-5,
    amount_relative_tolerance: float = 1e-5,
) -> dict[str, Any]:
    """Reject overlapping or incomplete 30-minute data via daily reconciliation."""

    intraday_frame = intraday.copy()
    daily_frame = daily.copy()
    intraday_frame["_timestamp"] = pd.to_datetime(intraday_frame["Date"], errors="coerce")
    daily_frame["_timestamp"] = pd.to_datetime(daily_frame["Date"], errors="coerce")
    if intraday_frame["_timestamp"].isna().any() or daily_frame["_timestamp"].isna().any():
        raise ValueError("Cannot reconcile bars with invalid timestamps")

    intraday_frame["_date"] = intraday_frame["_timestamp"].dt.strftime("%Y-%m-%d")
    daily_frame["_date"] = daily_frame["_timestamp"].dt.strftime("%Y-%m-%d")
    aggregate = intraday_frame.groupby("_date", sort=True).agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
        Amount=("Amount", "sum"),
    )
    reference = daily_frame.drop_duplicates("_date", keep="last").set_index("_date")
    common_days = sorted(set(aggregate.index).intersection(reference.index))
    if not common_days:
        raise ValueError("No common trading days between 30m and daily data")

    failures: list[str] = []
    for day in common_days:
        mismatched_fields: list[str] = []
        for field in ["Open", "High", "Low", "Close"]:
            if abs(float(aggregate.at[day, field]) - float(reference.at[day, field])) > price_tolerance:
                mismatched_fields.append(field)
        for field, tolerance in [
            ("Volume", volume_relative_tolerance),
            ("Amount", amount_relative_tolerance),
        ]:
            expected = float(reference.at[day, field])
            relative_error = abs(float(aggregate.at[day, field]) - expected) / max(abs(expected), 1.0)
            if relative_error > tolerance:
                mismatched_fields.append(field)
        if mismatched_fields:
            failures.append(f"{day}: {', '.join(mismatched_fields)}")
    if failures:
        raise ValueError("30m/daily reconciliation failed: " + "; ".join(failures))
    return {"matched_day_count": len(common_days), "matched_days": common_days}
