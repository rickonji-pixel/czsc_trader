"""Longbridge US-equity historical market-data adapter."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .bar_utils import INTRADAY_PERIOD_MINUTES, normalize_period
from .config import get_longbridge_credentials
from .errors import DataContractError, EmptyDataError
from .market_resolver import MARKET_US, detect_market, normalize_symbol_for_vendor


_NEW_YORK = ZoneInfo("America/New_York")
_MAX_REQUEST_RATE_SECONDS = 0.55
_INTRADAY_CHUNK_DAYS = {
    "1m": 2,
    "5m": 10,
    "15m": 30,
    "30m": 90,
    "daily": 365 * 3,
    "weekly": 365 * 15,
}
_ALL_SESSION_CHUNK_DAYS = {
    "1m": 1,
    "5m": 5,
    "15m": 14,
    "30m": 30,
    "daily": 365 * 3,
    "weekly": 365 * 15,
}


class RequestThrottle:
    """Share the documented history-request limit across one download batch."""

    def __init__(self, minimum_interval_seconds: float = _MAX_REQUEST_RATE_SECONDS) -> None:
        self.minimum_interval_seconds = float(minimum_interval_seconds)
        self._last_request_at: float | None = None

    def wait(self, sleeper: Callable[[float], None]) -> None:
        if self._last_request_at is None:
            return
        remaining = self.minimum_interval_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            sleeper(remaining)

    def mark(self) -> None:
        self._last_request_at = time.monotonic()


def _sdk_period(period: str):
    from longbridge.openapi import Period

    return {
        "1m": Period.Min_1,
        "5m": Period.Min_5,
        "15m": Period.Min_15,
        "30m": Period.Min_30,
        "daily": Period.Day,
        "weekly": Period.Week,
    }[period]


def _sdk_adjustment(adjustment: str):
    from longbridge.openapi import AdjustType

    value = str(adjustment).strip().lower()
    aliases = {
        "forward": "forward",
        "qfq": "forward",
        "none": "none",
        "no_adjust": "none",
        "noadjust": "none",
    }
    normalized = aliases.get(value)
    if normalized is None:
        raise ValueError("Longbridge adjustment must be forward/qfq or none")
    return (
        normalized,
        AdjustType.ForwardAdjust if normalized == "forward" else AdjustType.NoAdjust,
    )


def _sdk_trade_sessions(trade_sessions: str):
    from longbridge.openapi import TradeSessions

    value = str(trade_sessions).strip().lower()
    if value == "intraday":
        return value, TradeSessions.Intraday
    if value == "all":
        return value, TradeSessions.All
    raise ValueError("Longbridge trade_sessions must be intraday or all")


def create_quote_context(env_file: str | Path | None = None):
    """Create one quiet legacy-auth quote context for a caller-owned batch."""

    from longbridge.openapi import Config, QuoteContext

    credentials = get_longbridge_credentials(env_file)
    config = Config.from_apikey(
        credentials.app_key,
        credentials.app_secret,
        credentials.access_token,
        enable_overnight=False,
        enable_print_quote_packages=False,
    )
    return QuoteContext(config)


def _date_chunks(start: date, end: date, days: int) -> Iterator[tuple[date, date]]:
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=days - 1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _exchange_timestamp(value: datetime) -> datetime:
    """Convert the SDK's host-local naive datetime to New York exchange time."""

    if value.tzinfo is None:
        # Longbridge 5.1.0 renders its Unix timestamp in the host's local timezone
        # and drops tzinfo.  datetime.timestamp() applies that host's historical
        # timezone rules, recovering the underlying instant without hard-coding the
        # machine timezone.
        return datetime.fromtimestamp(value.timestamp(), _NEW_YORK).replace(tzinfo=None)
    return value.astimezone(_NEW_YORK).replace(tzinfo=None)


def _normalized_timestamp(value: datetime, period: str) -> datetime:
    timestamp = _exchange_timestamp(value)
    if period in INTRADAY_PERIOD_MINUTES:
        timestamp += timedelta(minutes=INTRADAY_PERIOD_MINUTES[period])
    return timestamp


def _bars_frame(bars: Iterable[Any], period: str) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    rows = []
    corrections: list[dict[str, str]] = []
    for bar in bars:
        timestamp = _normalized_timestamp(bar.timestamp, period)
        open_price = Decimal(bar.open)
        high_price = Decimal(bar.high)
        low_price = Decimal(bar.low)
        close_price = Decimal(bar.close)
        volume = int(bar.volume)
        turnover = Decimal(bar.turnover)
        if (
            min(open_price, high_price, low_price, close_price) <= 0
            or high_price < low_price
            or volume < 0
            or turnover < 0
        ):
            raise DataContractError("Longbridge returned invalid OHLCV relationships")
        if high_price < max(open_price, close_price) or low_price > min(
            open_price, close_price
        ):
            corrections.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "open": str(open_price),
                    "high": str(high_price),
                    "low": str(low_price),
                    "close": str(close_price),
                }
            )
            high_price = max(high_price, open_price, close_price)
            low_price = min(low_price, open_price, close_price)
        rows.append(
            {
                "Date": timestamp,
                "Open": float(open_price),
                "High": float(high_price),
                "Low": float(low_price),
                "Close": float(close_price),
                "Volume": volume,
                "Amount": float(turnover),
            }
        )
    if not rows:
        return (
            pd.DataFrame(
                columns=["Date", "Open", "High", "Low", "Close", "Volume", "Amount"]
            ),
            corrections,
        )
    frame = pd.DataFrame(rows).sort_values("Date").drop_duplicates("Date", keep="last")
    if period not in INTRADAY_PERIOD_MINUTES:
        frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d")
    else:
        frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    numeric = ["Open", "High", "Low", "Close", "Volume", "Amount"]
    if frame[numeric].isna().any().any():
        raise DataContractError("Longbridge returned null OHLCV values")
    return frame.reset_index(drop=True), corrections


def _request_bounds(start_date: str, end_date: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    if start.tz is not None:
        start = start.tz_convert(_NEW_YORK).tz_localize(None)
    if end.tz is not None:
        end = end.tz_convert(_NEW_YORK).tz_localize(None)
    if " " not in end_date and "T" not in end_date:
        end += pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    if start > end:
        raise ValueError("Longbridge history start must not follow end")
    return start, end


def fetch_stock_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str,
    period: str = "daily",
    *,
    adjustment: str = "forward",
    trade_sessions: str = "intraday",
    env_file: str | Path | None = None,
    quote_context: object | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    request_throttle: RequestThrottle | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return normalized US-equity bars through the Longbridge history API."""

    normalized_period = normalize_period(period)
    if detect_market(symbol) != MARKET_US:
        raise ValueError("Longbridge stock history currently supports US equities only")
    vendor_symbol = normalize_symbol_for_vendor(symbol, "longbridge", MARKET_US)
    adjustment_name, adjustment_type = _sdk_adjustment(adjustment)
    sessions_name, sessions_type = _sdk_trade_sessions(trade_sessions)
    context = quote_context or create_quote_context(env_file)
    throttle = request_throttle or RequestThrottle()
    request_start, request_end = _request_bounds(start_date, end_date)
    pieces: list[pd.DataFrame] = []
    envelope_corrections: list[dict[str, str]] = []
    request_count = 0
    for chunk_start, chunk_end in _date_chunks(
        request_start.date(),
        request_end.date(),
        (
            _ALL_SESSION_CHUNK_DAYS[normalized_period]
            if sessions_name == "all"
            else _INTRADAY_CHUNK_DAYS[normalized_period]
        ),
    ):
        throttle.wait(sleeper)
        try:
            bars = list(context.history_candlesticks_by_date(
                vendor_symbol,
                _sdk_period(normalized_period),
                adjustment_type,
                chunk_start,
                chunk_end,
                sessions_type,
            ))
        except Exception as exc:
            matched = re.search(
                r"out of minute candlestick limit begin date:(\d{4}-\d{2}-\d{2})",
                str(exc),
            )
            if matched:
                earliest = matched.group(1)
                raise DataContractError(
                    "Longbridge account minute-history entitlement begins on "
                    f"{earliest}; requested start was {request_start.date().isoformat()}",
                    earliest_available=earliest,
                    requested_start=request_start.date().isoformat(),
                ) from exc
            raise
        finally:
            throttle.mark()
        request_count += 1
        if len(bars) >= 1000:
            # The date API returns at most 1,000 bars, preferring the end of
            # the range. Exactly 1,000 may be complete, but cannot prove it;
            # never silently publish a possibly truncated training series.
            raise DataContractError(
                "Longbridge history date chunk reached the 1,000-bar limit; "
                "use a smaller range"
            )
        piece, corrections = _bars_frame(bars, normalized_period)
        envelope_corrections.extend(corrections)
        if not piece.empty:
            pieces.append(piece)
    if not pieces:
        raise EmptyDataError(
            f"Longbridge returned no data for {vendor_symbol} {normalized_period}"
        )
    frame = pd.concat(pieces, ignore_index=True)
    timestamps = pd.to_datetime(frame["Date"], errors="raise")
    frame = frame.loc[timestamps.between(request_start, request_end)].copy()
    frame = frame.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
    envelope_corrections = [
        item
        for item in envelope_corrections
        if request_start <= pd.Timestamp(item["timestamp"]) <= request_end
    ]
    if frame.empty:
        raise EmptyDataError(
            f"Longbridge returned no in-bound data for {vendor_symbol} {normalized_period}"
        )
    correction_payload = json.dumps(
        envelope_corrections,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return frame, {
        "vendor": "longbridge",
        "market": MARKET_US,
        "vendor_symbol": vendor_symbol,
        "period": normalized_period,
        "asset_type": "stock",
        "adjustment": adjustment_name,
        "adjustment_source": "longbridge",
        "trade_sessions": sessions_name,
        "enable_overnight": False,
        "timezone": "America/New_York",
        "timestamp_semantics": (
            "bar_close" if normalized_period in INTRADAY_PERIOD_MINUTES else "session_date"
        ),
        "vendor_timestamp_semantics": "bar_open",
        "request_count": request_count,
        "ohlc_envelope_correction_count": len(envelope_corrections),
        "ohlc_envelope_correction_sha256": sha256(correction_payload).hexdigest(),
        "primary_key": ["Date"],
    }


def fetch_stock_unadjusted_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    quote_context: object | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    request_throttle: RequestThrottle | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return unadjusted US daily bars for execution-price publication."""

    return fetch_stock_ohlcv(
        symbol,
        start_date,
        end_date,
        "daily",
        adjustment="none",
        trade_sessions="intraday",
        env_file=env_file,
        quote_context=quote_context,
        sleeper=sleeper,
        request_throttle=request_throttle,
    )


def fetch_us_trading_calendar(
    start_date: str,
    end_date: str,
    *,
    env_file: str | Path | None = None,
    quote_context: object | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    request_throttle: RequestThrottle | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Publish a full US calendar using SPY history plus the recent calendar API."""

    from longbridge.openapi import Market

    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    if start > end:
        raise ValueError("US trading calendar start must not follow end")
    context = quote_context or create_quote_context(env_file)
    source_start = start - timedelta(days=14)
    proxy_end = min(end, date.today())
    proxy_days: set[date] = set()
    if source_start <= proxy_end:
        proxy, _metadata = fetch_stock_unadjusted_daily(
            "SPY.US",
            source_start.isoformat(),
            proxy_end.isoformat(),
            env_file=env_file,
            quote_context=context,
            sleeper=sleeper,
            request_throttle=request_throttle,
        )
        proxy_days.update(pd.to_datetime(proxy["Date"], errors="raise").dt.date)

    recent_start = max(source_start, date.today() - timedelta(days=364))
    official_days: set[date] = set()
    half_days: set[date] = set()
    if recent_start <= end:
        cursor = recent_start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=27))
            response = context.trading_days(Market.US, cursor, chunk_end)
            official_days.update(response.trading_days)
            half_days.update(response.half_trading_days)
            cursor = chunk_end + timedelta(days=1)
        proxy_days.difference_update(
            item.date() for item in pd.date_range(recent_start, end, freq="D")
        )
        proxy_days.update(official_days)

    full_range = pd.date_range(start, end, freq="D")
    prior = sorted(item for item in proxy_days if item < start)
    previous = prior[-1] if prior else None
    rows: list[dict[str, object]] = []
    for timestamp in full_range:
        current = timestamp.date()
        is_open = current in proxy_days
        rows.append(
            {
                "Date": timestamp.strftime("%Y-%m-%d"),
                "IsOpen": int(is_open),
                "PreviousTradingDate": (
                    previous.isoformat() if previous is not None else pd.NaT
                ),
            }
        )
        if is_open:
            previous = current
    return pd.DataFrame(rows), {
        "vendor": "longbridge",
        "exchange": "US",
        "frequency": "daily",
        "calendar_proxy": "SPY.US",
        "calendar_api_recent_days": recent_start <= end,
        "half_trading_days": sorted(item.isoformat() for item in half_days),
        "primary_key": ["Date"],
    }
