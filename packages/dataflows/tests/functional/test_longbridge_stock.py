from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from dataflows import DataRequest, DataStatus, Dataflows, Dataset
from dataflows.config import get_longbridge_credentials
from dataflows.errors import DataContractError, EmptyDataError
from dataflows.longbridge_stock import (
    _date_chunks,
    fetch_stock_ohlcv,
    fetch_us_trading_calendar,
)
from dataflows.market_resolver import normalize_symbol_for_vendor
from dataflows import longbridge_stock


@dataclass(frozen=True)
class FakeCandlestick:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    turnover: Decimal


class FakeQuoteContext:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def history_candlesticks_by_date(self, *args):
        self.calls.append(args)
        return self.bars


class FakeCalendarQuoteContext(FakeQuoteContext):
    def trading_days(self, market, begin, end):
        return SimpleNamespace(
            trading_days=[
                item.date()
                for item in pd.date_range(begin, end, freq="D")
                if item.weekday() < 5
            ],
            half_trading_days=[],
        )


def test_legacy_credentials_prefer_process_environment(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LONGBRIDGE_APP_KEY=file-key\n"
        "LONGBRIDGE_APP_SECRET=file-secret\n"
        "LONGBRIDGE_ACCESS_TOKEN=file-token\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LONGBRIDGE_APP_KEY", "process-key")

    credentials = get_longbridge_credentials(env_file)

    assert credentials.app_key == "process-key"
    assert credentials.app_secret == "file-secret"
    assert credentials.access_token == "file-token"


def test_legacy_credentials_report_names_but_not_values(tmp_path, monkeypatch) -> None:
    for name in (
        "LONGBRIDGE_APP_KEY",
        "LONGBRIDGE_APP_SECRET",
        "LONGBRIDGE_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("LONGBRIDGE_APP_KEY=private-value\n", encoding="utf-8")

    with pytest.raises(ValueError) as captured:
        get_longbridge_credentials(env_file)

    assert "LONGBRIDGE_APP_SECRET" in str(captured.value)
    assert "LONGBRIDGE_ACCESS_TOKEN" in str(captured.value)
    assert "private-value" not in str(captured.value)


def test_longbridge_symbol_uses_vendor_suffix() -> None:
    assert normalize_symbol_for_vendor("AAPL", "longbridge") == "AAPL.US"
    assert normalize_symbol_for_vendor("US.BRK.B", "longbridge") == "BRK.B.US"
    assert normalize_symbol_for_vendor("BRK.B.US", "longbridge") == "BRK.B.US"


def test_reversed_history_range_fails_before_any_vendor_request() -> None:
    context = FakeQuoteContext([])
    with pytest.raises(ValueError, match="start must not follow end"):
        fetch_stock_ohlcv(
            "MU.US", "2026-09-24", "2026-09-23", "30m",
            quote_context=context,
        )
    assert context.calls == []


def test_history_chunk_at_vendor_cap_fails_instead_of_publishing_truncated_data() -> None:
    bar = FakeCandlestick(
        datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
        Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"),
        100, Decimal("10000"),
    )
    context = FakeQuoteContext([bar] * 1000)
    with pytest.raises(DataContractError, match="1,000-bar limit"):
        fetch_stock_ohlcv(
            "MU.US", "2024-01-02", "2024-01-02", "30m",
            quote_context=context,
        )


def test_longbridge_30m_bars_are_exchange_local_bar_close_times() -> None:
    context = FakeQuoteContext(
        [
            FakeCandlestick(
                datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
                Decimal("187.15"),
                Decimal("188.44"),
                Decimal("186.90"),
                Decimal("188.10"),
                1234,
                Decimal("231234.56"),
            ),
            FakeCandlestick(
                datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc),
                Decimal("188.10"),
                Decimal("188.50"),
                Decimal("187.80"),
                Decimal("188.20"),
                1000,
                Decimal("188150.00"),
            ),
        ]
    )

    frame, metadata = fetch_stock_ohlcv(
        "AAPL",
        "2024-01-02",
        "2024-01-02",
        "30m",
        quote_context=context,
        sleeper=lambda ignored: None,
    )

    assert frame["Date"].tolist() == ["2024-01-02 10:00:00", "2024-01-02 10:30:00"]
    assert frame.columns.tolist() == [
        "Date",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "Amount",
    ]
    assert metadata["vendor_symbol"] == "AAPL.US"
    assert metadata["adjustment"] == "forward"
    assert metadata["timezone"] == "America/New_York"
    assert metadata["timestamp_semantics"] == "bar_close"
    assert metadata["vendor_timestamp_semantics"] == "bar_open"
    assert metadata["enable_overnight"] is False
    assert len(context.calls) == 1


def test_account_minute_history_limit_is_a_structured_contract_failure() -> None:
    class LimitedContext:
        def history_candlesticks_by_date(self, *args):
            raise RuntimeError(
                "OpenApiException: out of minute candlestick limit begin date:2023-09-01"
            )

    with pytest.raises(DataContractError) as captured:
        fetch_stock_ohlcv(
            "AAPL.US",
            "2003-09-01",
            "2003-09-02",
            "30m",
            quote_context=LimitedContext(),
        )

    assert captured.value.code == "DATA_CONTRACT_MISMATCH"
    assert captured.value.context["earliest_available"] == "2023-09-01"
    assert "2003-09-01" in str(captured.value)


def test_ohlc_envelope_correction_is_explicit_and_hashed() -> None:
    context = FakeQuoteContext(
        [
            FakeCandlestick(
                datetime(2026, 5, 27, 13, 30, tzinfo=timezone.utc),
                Decimal("308.330"),
                Decimal("312.340"),
                Decimal("308.380"),
                Decimal("311.909"),
                3349143,
                Decimal("1041065119.478"),
            )
        ]
    )

    frame, metadata = fetch_stock_ohlcv(
        "AAPL.US",
        "2026-05-27",
        "2026-05-27",
        "30m",
        adjustment="none",
        quote_context=context,
    )

    assert frame.iloc[0]["Low"] == frame.iloc[0]["Open"] == 308.33
    assert metadata["ohlc_envelope_correction_count"] == 1
    assert len(metadata["ohlc_envelope_correction_sha256"]) == 64


def test_long_history_chunking_never_overlaps() -> None:
    chunks = list(
        _date_chunks(
            pd.Timestamp("2020-01-01").date(),
            pd.Timestamp("2020-07-01").date(),
            90,
        )
    )

    assert chunks[0][0].isoformat() == "2020-01-01"
    assert chunks[-1][1].isoformat() == "2020-07-01"
    assert all((right[0] - left[1]).days == 1 for left, right in zip(chunks, chunks[1:]))
    assert all((end - start).days < 90 for start, end in chunks)


def test_all_session_one_minute_requests_use_one_calendar_day_chunks() -> None:
    context = FakeQuoteContext([])

    with pytest.raises(EmptyDataError):
        fetch_stock_ohlcv(
            "AAPL.US",
            "2024-01-02",
            "2024-01-03",
            "1m",
            trade_sessions="all",
            quote_context=context,
            sleeper=lambda ignored: None,
        )

    assert len(context.calls) == 2
    assert context.calls[0][3].isoformat() == "2024-01-02"
    assert context.calls[0][4].isoformat() == "2024-01-02"
    assert context.calls[1][3].isoformat() == "2024-01-03"
    assert context.calls[1][4].isoformat() == "2024-01-03"


def test_us_calendar_combines_daily_proxy_with_recent_calendar_api() -> None:
    today = date.today()
    start = today - timedelta(days=2)
    end = today + timedelta(days=2)
    proxy_date = today - timedelta(days=10)
    context = FakeCalendarQuoteContext(
        [
            FakeCandlestick(
                datetime.combine(proxy_date, datetime.min.time(), timezone.utc),
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
                100,
                Decimal("10000"),
            )
        ]
    )

    frame, metadata = fetch_us_trading_calendar(
        start.isoformat(),
        end.isoformat(),
        quote_context=context,
        sleeper=lambda ignored: None,
    )

    assert frame["Date"].tolist() == [
        item.strftime("%Y-%m-%d") for item in pd.date_range(start, end, freq="D")
    ]
    expected = [int(item.weekday() < 5) for item in pd.date_range(start, end, freq="D")]
    assert frame["IsOpen"].tolist() == expected
    assert metadata["vendor"] == "longbridge"
    assert metadata["calendar_proxy"] == "SPY.US"
    assert metadata["primary_key"] == ["Date"]


def test_dfls_routes_only_explicit_longbridge_stock_requests(monkeypatch) -> None:
    observed = {}

    def fake_fetch(symbol, start, end, period, **kwargs):
        observed.update(
            symbol=symbol,
            start=start,
            end=end,
            period=period,
            kwargs=kwargs,
        )
        times = pd.date_range("2024-01-02 10:00:00", "2024-01-02 16:00:00", freq="30min")
        return pd.DataFrame(
            {
                "Date": times.strftime("%Y-%m-%d %H:%M:%S"),
                "Open": [1.0] * len(times),
                "High": [1.1] * len(times),
                "Low": [0.9] * len(times),
                "Close": [1.05] * len(times),
                "Volume": [10] * len(times),
                "Amount": [10.5] * len(times),
            }
        ), {"vendor": "longbridge", "market": "us", "trade_sessions": "intraday", "primary_key": ["Date"]}

    monkeypatch.setattr(longbridge_stock, "fetch_stock_ohlcv", fake_fetch)
    result = Dataflows().fetch(
        DataRequest(
            Dataset.STOCK_OHLCV,
            "AAPL.US",
            "2024-01-02",
            "2024-01-02",
            "2024-01-02",
            "30m",
            {
                "vendor": "longbridge",
                "adjustment": "none",
                "trade_sessions": "intraday",
                "env_file": ".env",
            },
        )
    )

    assert result.status is DataStatus.READY
    assert result.identity is not None and result.identity.source == "longbridge"
    assert observed["symbol"] == "AAPL.US"
    assert observed["period"] == "30m"
    assert observed["kwargs"]["adjustment"] == "none"
    assert observed["kwargs"]["trade_sessions"] == "intraday"


def test_dfls_rejects_unknown_stock_vendor_without_fallback() -> None:
    result = Dataflows().fetch(
        DataRequest(
            Dataset.STOCK_OHLCV,
            "AAPL.US",
            "2024-01-02",
            "2024-01-02",
            None,
            "daily",
            {"vendor": "unknown"},
        )
    )

    assert result.status is DataStatus.FAILED
    assert result.error is not None
    assert result.error.code == "DATA_CONTRACT_MISMATCH"
