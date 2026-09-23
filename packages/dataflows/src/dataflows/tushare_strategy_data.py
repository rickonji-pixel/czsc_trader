"""Canonical Tushare inputs required by the active frozen strategies."""

from __future__ import annotations

from collections.abc import Callable
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


def _fetch_yearly(
    fetch: Callable[[str, str], object],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Fetch annual slices so vendor row limits cannot truncate long histories."""

    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    frames: list[pd.DataFrame] = []
    for year in range(start.year, end.year + 1):
        chunk_start = max(start, pd.Timestamp(year=year, month=1, day=1))
        chunk_end = min(end, pd.Timestamp(year=year, month=12, day=31))
        value = fetch(chunk_start.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d"))
        frame = pd.DataFrame() if value is None else pd.DataFrame(value)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _canonicalize_monthly(
    value: object,
    *,
    dataset: str,
    columns: dict[str, str],
    numeric_columns: tuple[str, ...],
) -> pd.DataFrame:
    frame = pd.DataFrame() if value is None else pd.DataFrame(value).copy()
    required = {"month", *columns}
    missing = sorted(required.difference(frame.columns))
    if frame.empty:
        raise EmptyDataError(f"Tushare returned no data for {dataset}")
    if missing:
        raise DataContractError(
            f"Tushare {dataset} response is missing required fields",
            missing_fields=missing,
        )
    output = frame[["month", *columns]].rename(columns=columns)
    output["Date"] = pd.PeriodIndex(
        output.pop("month").astype(str), freq="M"
    ).to_timestamp(how="end").normalize()
    for column in numeric_columns:
        output[column] = pd.to_numeric(output[column], errors="raise")
    return output[["Date", *columns.values()]].sort_values("Date").reset_index(drop=True)


def fetch_shibor_daily(
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    client = _client(pro, env_file)
    dataframe = _canonicalize(
        _fetch_yearly(
            lambda start, end: client.shibor(
                start_date=start,
                end_date=end,
            ),
            start_date,
            end_date,
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
        "maximum_start_lag_days": 10,
    }


def fetch_us_real_yield_daily(
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    client = _client(pro, env_file)
    dataframe = _canonicalize(
        _fetch_yearly(
            lambda start, end: client.us_trycr(
                start_date=start,
                end_date=end,
                fields="date,y5,y7,y10,y20,y30",
            ),
            start_date,
            end_date,
        ),
        dataset="US real Treasury yield daily",
        date_column="date",
        columns={
            "y5": "RealYield5YPercent",
            "y7": "RealYield7YPercent",
            "y10": "RealYield10YPercent",
            "y20": "RealYield20YPercent",
            "y30": "RealYield30YPercent",
        },
        numeric_columns=(
            "RealYield5YPercent",
            "RealYield7YPercent",
            "RealYield10YPercent",
            "RealYield20YPercent",
            "RealYield30YPercent",
        ),
    )
    return dataframe, {
        "vendor": "tushare",
        "frequency": "daily",
        "primary_key": ["Date"],
        "unit": "percent",
        "availability_rule": "source date no later than prior China trading day",
    }


def fetch_fxcm_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    client = _client(pro, env_file)
    dataframe = _canonicalize(
        _fetch_yearly(
            lambda start, end: client.fx_daily(
                ts_code=symbol,
                start_date=start,
                end_date=end,
                fields=(
                    "ts_code,trade_date,bid_open,bid_close,bid_high,bid_low,"
                    "ask_open,ask_close,ask_high,ask_low,tick_qty"
                ),
            ),
            start_date,
            end_date,
        ),
        dataset=f"FXCM daily {symbol}",
        date_column="trade_date",
        columns={
            "bid_open": "BidOpen",
            "bid_high": "BidHigh",
            "bid_low": "BidLow",
            "bid_close": "BidClose",
            "ask_open": "AskOpen",
            "ask_high": "AskHigh",
            "ask_low": "AskLow",
            "ask_close": "AskClose",
            "tick_qty": "TickQuantity",
        },
        numeric_columns=(
            "BidOpen",
            "BidHigh",
            "BidLow",
            "BidClose",
            "AskOpen",
            "AskHigh",
            "AskLow",
            "AskClose",
            "TickQuantity",
        ),
    )
    return dataframe, {
        "vendor": "tushare",
        "vendor_symbol": symbol,
        "frequency": "daily",
        "primary_key": ["Date"],
        "vendor_timezone": "GMT",
        "availability_rule": "GMT source date must be strictly earlier than China decision session",
        "maximum_start_lag_days": 10,
    }


def fetch_usdcnh_daily(
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Retain the stable USDCNH dataset over the generic FXCM adapter."""

    dataframe, metadata = fetch_fxcm_daily(
        "USDCNH.FXCM",
        start_date,
        end_date,
        env_file=env_file,
        pro=pro,
    )
    metadata = dict(metadata)
    metadata["availability_rule"] = "use at least one completed China trading day lag"
    metadata.pop("maximum_start_lag_days", None)
    return dataframe, metadata


def fetch_sge_gold_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    client = _client(pro, env_file)
    dataframe = _canonicalize(
        _fetch_yearly(
            lambda start, end: client.sge_daily(
                ts_code=symbol,
                start_date=start,
                end_date=end,
                fields=(
                    "ts_code,trade_date,close,open,high,low,price_avg,change,"
                    "pct_change,vol,amount"
                ),
            ),
            start_date,
            end_date,
        ),
        dataset=f"SGE gold daily {symbol}",
        date_column="trade_date",
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "price_avg": "AveragePrice",
            "change": "Change",
            "pct_change": "PercentChange",
            "vol": "Volume",
            "amount": "Amount",
        },
        numeric_columns=(
            "Open",
            "High",
            "Low",
            "Close",
            "AveragePrice",
            "Change",
            "PercentChange",
            "Volume",
            "Amount",
        ),
    )
    dataframe["PercentChange"] /= 100.0
    return dataframe, {
        "vendor": "tushare",
        "vendor_symbol": symbol,
        "frequency": "daily",
        "primary_key": ["Date"],
        "percent_change_unit": "decimal_return",
        "session_rule": "prior night session plus current day session through 15:30",
        "availability_rule": "use prior trading day until publication time is governed",
        "price_validation_tolerance": "0.011 CNY per gram for one-tick vendor rounding",
    }


def fetch_domestic_index_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    dataframe = _canonicalize(
        _client(pro, env_file).index_daily(
            ts_code=symbol,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            fields=(
                "ts_code,trade_date,close,open,high,low,pre_close,change,"
                "pct_chg,vol,amount"
            ),
        ),
        dataset=f"domestic index daily {symbol}",
        date_column="trade_date",
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "pre_close": "PreviousClose",
            "change": "Change",
            "pct_chg": "PercentChange",
            "vol": "Volume",
            "amount": "Amount",
        },
        numeric_columns=(
            "Open",
            "High",
            "Low",
            "Close",
            "PreviousClose",
            "Change",
            "PercentChange",
            "Volume",
            "Amount",
        ),
    )
    dataframe["PercentChange"] /= 100.0
    return dataframe, {
        "vendor": "tushare",
        "vendor_symbol": symbol,
        "frequency": "daily",
        "primary_key": ["Date"],
        "percent_change_unit": "decimal_return",
        "availability_rule": "current session after market close",
    }


def fetch_cn_cpi_monthly(
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    start_month = pd.Timestamp(start_date).strftime("%Y%m")
    end_month = pd.Timestamp(end_date).strftime("%Y%m")
    dataframe = _canonicalize_monthly(
        _client(pro, env_file).cn_cpi(start_m=start_month, end_m=end_month),
        dataset="China CPI monthly",
        columns={
            "nt_val": "NationalIndex",
            "nt_yoy": "NationalYoYPercent",
            "nt_mom": "NationalMoMPercent",
            "nt_accu": "NationalAccumulatedPercent",
        },
        numeric_columns=(
            "NationalIndex",
            "NationalYoYPercent",
            "NationalMoMPercent",
            "NationalAccumulatedPercent",
        ),
    )
    return dataframe, _monthly_metadata("cn_cpi")


def fetch_cn_ppi_monthly(
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    start_month = pd.Timestamp(start_date).strftime("%Y%m")
    end_month = pd.Timestamp(end_date).strftime("%Y%m")
    dataframe = _canonicalize_monthly(
        _client(pro, env_file).cn_ppi(start_m=start_month, end_m=end_month),
        dataset="China PPI monthly",
        columns={
            "ppi_yoy": "ProducerYoYPercent",
            "ppi_mom": "ProducerMoMPercent",
            "ppi_accu": "ProducerAccumulatedPercent",
        },
        numeric_columns=(
            "ProducerYoYPercent",
            "ProducerMoMPercent",
            "ProducerAccumulatedPercent",
        ),
    )
    return dataframe, _monthly_metadata("cn_ppi")


def fetch_cn_money_monthly(
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    start_month = pd.Timestamp(start_date).strftime("%Y%m")
    end_month = pd.Timestamp(end_date).strftime("%Y%m")
    dataframe = _canonicalize_monthly(
        _client(pro, env_file).cn_m(start_m=start_month, end_m=end_month),
        dataset="China money supply monthly",
        columns={
            "m0": "M0",
            "m0_yoy": "M0YoYPercent",
            "m0_mom": "M0MoMPercent",
            "m1": "M1",
            "m1_yoy": "M1YoYPercent",
            "m1_mom": "M1MoMPercent",
            "m2": "M2",
            "m2_yoy": "M2YoYPercent",
            "m2_mom": "M2MoMPercent",
        },
        numeric_columns=(
            "M0",
            "M0YoYPercent",
            "M0MoMPercent",
            "M1",
            "M1YoYPercent",
            "M1MoMPercent",
            "M2",
            "M2YoYPercent",
            "M2MoMPercent",
        ),
    )
    return dataframe, _monthly_metadata("cn_m")


def _monthly_metadata(vendor_interface: str) -> dict[str, Any]:
    return {
        "vendor": "tushare",
        "vendor_interface": vendor_interface,
        "frequency": "monthly",
        "primary_key": ["Date"],
        "reference_date_rule": "calendar month end",
        "availability_rule": "reference month M usable from first China session of M+2",
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
        _fetch_yearly(
            lambda start, end: _client(pro, env_file).index_global(
                ts_code=symbol,
                start_date=start,
                end_date=end,
            ),
            start_date,
            end_date,
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
        "availability_rule": "US close date must be strictly earlier than China decision session",
        "maximum_start_lag_days": 10,
    }


def fetch_vix_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    pro: object | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if symbol.upper() != "VIX":
        raise DataContractError("VIX daily dataset only supports symbol VIX", symbol=symbol)
    client = _client(pro, env_file)
    dataframe = _canonicalize(
        _fetch_yearly(
            lambda start, end: client.vix_index(
                start_date=start,
                end_date=end,
                fields="trade_date,open,high,low,close,pct_change",
            ),
            start_date,
            end_date,
        ),
        dataset="VIX daily",
        date_column="trade_date",
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "pct_change": "PercentChange",
        },
        numeric_columns=(
            "Open",
            "High",
            "Low",
            "Close",
            "PercentChange",
        ),
    )
    dataframe["PercentChange"] /= 100.0
    return dataframe, {
        "vendor": "tushare",
        "vendor_symbol": "VIX",
        "frequency": "daily",
        "primary_key": ["Date"],
        "percent_change_unit": "decimal_return",
        "availability_rule": "US close date must be strictly earlier than China decision session",
        "maximum_start_lag_days": 10,
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
