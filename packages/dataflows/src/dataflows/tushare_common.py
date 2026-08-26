"""Shared Tushare client construction."""

from __future__ import annotations

from pathlib import Path

import tushare as ts

from .config import get_tushare_token


def get_tushare_module(env_file: str | Path | None = None):
    token = get_tushare_token(env_file)
    ts.set_token(token)
    return ts


def get_tushare_pro(env_file: str | Path | None = None):
    module = get_tushare_module(env_file)
    return module.pro_api(get_tushare_token(env_file))


def fetch_instrument_name(
    symbol: str,
    asset_type: str,
    *,
    env_file: str | Path | None = None,
) -> str:
    """Return the unique Tushare Chinese short name for an A-share instrument."""
    pro = get_tushare_pro(env_file)
    if asset_type == "etf":
        frame = pro.fund_basic(
            market="E",
            ts_code=symbol,
            fields="ts_code,name",
        )
    elif asset_type == "stock":
        frame = pro.stock_basic(ts_code=symbol, fields="ts_code,name")
    else:
        raise ValueError("asset_type must be stock or etf")
    if frame is None or len(frame) != 1:
        count = 0 if frame is None else len(frame)
        raise ValueError(f"Tushare returned {count} instrument names for {symbol}")
    name = str(frame.iloc[0].get("name", "")).strip()
    if not name:
        raise ValueError(f"Tushare returned an empty instrument name for {symbol}")
    return name
