"""Canonical Tushare inputs required by the active frozen strategies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .errors import DataContractError, EmptyDataError
from .tushare_common import get_tushare_pro


def _client(pro: object | None, env_file: str | Path | None) -> object:
    return pro if pro is not None else get_tushare_pro(env_file)


def _canonicalize(
    value: object,
    *,
    dataset: str,
    date_column: str,
    columns: dict[str, str],
    numeric_columns: tuple[str, ...],
) -> pd.DataFrame:
    frame = pd.DataFrame() if value is None else pd.DataFrame(value).copy()
    required = {date_column, *columns}
    missing = sorted(required.difference(frame.columns))
    if frame.empty:
        raise EmptyDataError(f"Tushare returned no data for {dataset}")
    if missing:
        raise DataContractError(
            f"Tushare {dataset} response is missing required fields",
            missing_fields=missing,
        )
    output = frame[[date_column, *columns]].rename(columns={date_column: "Date", **columns})
    output["Date"] = pd.to_datetime(output["Date"].astype(str), errors="raise").dt.normalize()
    for column in numeric_columns:
        output[column] = pd.to_numeric(output[column], errors="raise")
    return output.sort_values(
        ["Date", *[item for item in output.columns if item != "Date"]]
    ).reset_index(drop=True)


def fetch_shibor_daily(
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    dataframe = _canonicalize(
        _client(pro, env_file).shibor(
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
        ),
        dataset="SHIBOR daily",
        date_column="date",
        columns={"on": "OvernightRate"},
        numeric_columns=("OvernightRate",),
    )
    return dataframe, {
        "vendor": "tushare",
        "frequency": "daily",
        "primary_key": ["Date"],
    }


def fetch_index_daily_basic(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    dataframe = _canonicalize(
        _client(pro, env_file).index_dailybasic(
            ts_code=symbol,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            fields="ts_code,trade_date,turnover_rate_f",
        ),
        dataset=f"index daily basic {symbol}",
        date_column="trade_date",
        columns={"turnover_rate_f": "TurnoverRateFreeFloat"},
        numeric_columns=("TurnoverRateFreeFloat",),
    )
    return dataframe, {
        "vendor": "tushare",
        "vendor_symbol": symbol,
        "frequency": "daily",
        "primary_key": ["Date"],
    }


def fetch_etf_share_size(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    dataframe = _canonicalize(
        _client(pro, env_file).etf_share_size(
            ts_code=symbol,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            fields="trade_date,ts_code,total_share",
        ),
        dataset=f"ETF share size {symbol}",
        date_column="trade_date",
        columns={"total_share": "TotalShare"},
        numeric_columns=("TotalShare",),
    )
    return dataframe, {
        "vendor": "tushare",
        "vendor_symbol": symbol,
        "frequency": "daily",
        "primary_key": ["Date"],
        "availability_rule": "T+1 08:30 Asia/Shanghai",
    }


def fetch_global_index_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    dataframe = _canonicalize(
        _client(pro, env_file).index_global(
            ts_code=symbol,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
        ),
        dataset=f"global index daily {symbol}",
        date_column="trade_date",
        columns={"pct_chg": "PercentChange"},
        numeric_columns=("PercentChange",),
    )
    dataframe["PercentChange"] /= 100.0
    return dataframe, {
        "vendor": "tushare",
        "vendor_symbol": symbol,
        "frequency": "daily",
        "primary_key": ["Date"],
        "unit": "decimal_return",
    }


def fetch_index_constituent_weight(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    dataframe = _canonicalize(
        _client(pro, env_file).index_weight(
            index_code=symbol,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            fields="index_code,con_code,trade_date,weight",
        ),
        dataset=f"index constituent weight {symbol}",
        date_column="trade_date",
        columns={"con_code": "ConstituentSymbol", "weight": "Weight"},
        numeric_columns=("Weight",),
    )
    return dataframe, {
        "vendor": "tushare",
        "vendor_symbol": symbol,
        "frequency": "snapshot",
        "primary_key": ["Date", "ConstituentSymbol"],
    }


def fetch_stock_moneyflow(
    start_date: str,
    end_date: str,
    *,
    symbol: str | None = None,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if symbol is None and pd.Timestamp(start_date) != pd.Timestamp(end_date):
        raise DataContractError("all-market stock moneyflow requests must cover exactly one day")
    parameters = {
        "fields": "ts_code,trade_date,net_mf_amount",
        "start_date": start_date.replace("-", ""),
        "end_date": end_date.replace("-", ""),
    }
    if symbol is not None:
        parameters["ts_code"] = symbol
    dataframe = _canonicalize(
        _client(pro, env_file).moneyflow(**parameters),
        dataset=f"stock moneyflow {symbol or 'all-market'}",
        date_column="trade_date",
        columns={"ts_code": "Symbol", "net_mf_amount": "NetMoneyflowAmount"},
        numeric_columns=("NetMoneyflowAmount",),
    )
    return dataframe, {
        "vendor": "tushare",
        "vendor_symbol": symbol,
        "frequency": "daily",
        "primary_key": ["Date", "Symbol"],
    }


def fetch_stock_moneyflow_sessions(
    trading_dates: tuple[str, ...],
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fetch all-market moneyflow for explicit trading sessions.

    Tushare exposes the all-market snapshot one session at a time.  Keeping the
    session list explicit prevents weekend/holiday requests and makes the
    publication boundary auditable.
    """

    if not trading_dates:
        raise DataContractError("stock moneyflow trading_dates must not be empty")
    client = _client(pro, env_file)
    frames: list[pd.DataFrame] = []
    for value in trading_dates:
        session = pd.Timestamp(value).strftime("%Y%m%d")
        frame = _canonicalize(
            client.moneyflow(
                trade_date=session,
                fields="ts_code,trade_date,net_mf_amount",
            ),
            dataset=f"all-market stock moneyflow {session}",
            date_column="trade_date",
            columns={"ts_code": "Symbol", "net_mf_amount": "NetMoneyflowAmount"},
            numeric_columns=("NetMoneyflowAmount",),
        )
        frames.append(frame)
    dataframe = (
        pd.concat(frames, ignore_index=True).sort_values(["Date", "Symbol"]).reset_index(drop=True)
    )
    return dataframe, {
        "vendor": "tushare",
        "frequency": "daily",
        "primary_key": ["Date", "Symbol"],
        "requested_trading_dates": list(trading_dates),
    }


def fetch_trading_calendar(
    exchange: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    dataframe = _canonicalize(
        _client(pro, env_file).trade_cal(
            exchange=exchange,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            fields="exchange,cal_date,is_open,pretrade_date",
        ),
        dataset=f"trading calendar {exchange}",
        date_column="cal_date",
        columns={"is_open": "IsOpen", "pretrade_date": "PreviousTradingDate"},
        numeric_columns=("IsOpen",),
    )
    dataframe["PreviousTradingDate"] = pd.to_datetime(
        dataframe["PreviousTradingDate"].astype(str), errors="coerce"
    ).dt.normalize()
    return dataframe, {
        "vendor": "tushare",
        "exchange": exchange,
        "frequency": "daily",
        "primary_key": ["Date"],
    }
