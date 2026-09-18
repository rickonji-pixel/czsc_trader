"""Shared Tushare client construction."""

from __future__ import annotations

from pathlib import Path
from datetime import date, timedelta

import tushare as ts

from .config import get_tushare_token


def get_tushare_module(env_file: str | Path | None = None):
    token = get_tushare_token(env_file)
    ts.set_token(token)
    return ts


def get_tushare_pro(env_file: str | Path | None = None):
    # ``ts.set_token`` persists ``tk.csv`` in the user profile. Pro clients
    # already accept the token explicitly, so avoid that unrelated filesystem
    # side effect and keep DFLS execution inside the repository boundary.
    return ts.pro_api(get_tushare_token(env_file))


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


def fetch_next_trading_session(
    after: date,
    *,
    env_file: str | Path | None = None,
    pro=None,
) -> tuple[date, dict[str, str]]:
    """Return the first open SSE session after a completed trading date."""
    client = pro or get_tushare_pro(env_file)
    start = after + timedelta(days=1)
    end = after + timedelta(days=40)
    frame = client.trade_cal(
        exchange="SSE",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        is_open="1",
        fields="exchange,cal_date,is_open",
    )
    if frame is None or frame.empty:
        raise ValueError(f"Tushare returned no open SSE session after {after}")
    sessions = sorted(date.fromisoformat(str(value)) for value in frame["cal_date"])
    return sessions[0], {"vendor": "tushare", "exchange": "SSE"}
