from __future__ import annotations

import pandas as pd
import pytest

import dataflows.tushare_etf as tushare_etf
import dataflows.tushare_stock as tushare_stock
import dataflows.bar_utils as bar_utils


SAMPLE = pd.DataFrame(
    {
        "Date": ["2026-08-21"],
        "Open": [100.0],
        "High": [101.0],
        "Low": [99.0],
        "Close": [100.5],
        "Volume": [1000.0],
        "Amount": [100000.0],
    }
)


def test_fetch_stock_ohlcv_returns_dataframe_and_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tushare_stock,
        "_fetch_tushare_ohlcv",
        lambda *args, **kwargs: (SAMPLE, "a_share", "600519.SH"),
    )
    monkeypatch.setattr(
        tushare_stock,
        "_fetch_hfq_factors",
        lambda *args, **kwargs: pd.DataFrame(
            {"Date": ["2026-08-21"], "AdjFactor": [1.0]}
        ),
        raising=False,
    )

    frame, metadata = tushare_stock.fetch_stock_ohlcv(
        "600519.SH", "2026-08-01", "2026-08-21", "daily"
    )

    pd.testing.assert_frame_equal(frame, SAMPLE)
    assert metadata == {
        "vendor": "tushare",
        "market": "a_share",
        "vendor_symbol": "600519.SH",
        "period": "daily",
        "asset_type": "stock",
        "adjustment": "hfq",
        "adjustment_factor_source": "adj_factor",
        "adjustment_factor_sha256": metadata["adjustment_factor_sha256"],
    }


def test_fetch_etf_ohlcv_returns_dataframe_and_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tushare_etf,
        "_fetch_tushare_etf_ohlcv",
        lambda *args, **kwargs: (SAMPLE, "a_share", "510300.SH"),
    )
    monkeypatch.setattr(
        tushare_etf,
        "_fetch_hfq_factors",
        lambda *args, **kwargs: pd.DataFrame(
            {"Date": ["2026-08-21"], "AdjFactor": [1.0]}
        ),
        raising=False,
    )

    frame, metadata = tushare_etf.fetch_etf_ohlcv(
        "510300.SH", "2026-08-01", "2026-08-21", "weekly"
    )

    pd.testing.assert_frame_equal(frame, SAMPLE)
    assert metadata["asset_type"] == "etf"
    assert metadata["period"] == "weekly"
    assert metadata["vendor_symbol"] == "510300.SH"


def test_fetch_dataframe_api_propagates_vendor_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> tuple[pd.DataFrame, str, str]:
        raise RuntimeError("vendor unavailable")

    monkeypatch.setattr(tushare_stock, "_fetch_tushare_ohlcv", fail)

    with pytest.raises(RuntimeError, match="vendor unavailable"):
        tushare_stock.fetch_stock_ohlcv("600519.SH", "2026-08-01", "2026-08-21", "daily")


def test_fetch_dataframe_api_rejects_empty_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tushare_etf,
        "_fetch_tushare_etf_ohlcv",
        lambda *args, **kwargs: (pd.DataFrame(), "a_share", "510300.SH"),
    )

    with pytest.raises(ValueError, match="no data"):
        tushare_etf.fetch_etf_ohlcv("510300.SH", "2026-08-01", "2026-08-21", "daily")


def test_hfq_adjustment_multiplies_prices_divides_volume_and_keeps_amount() -> None:
    """Catch a wrong price/volume direction or mutation of actual turnover."""
    bars = pd.DataFrame(
        {
            "Date": ["2026-03-27", "2026-03-30"],
            "Open": [10.0, 5.1],
            "High": [10.2, 5.3],
            "Low": [9.8, 5.0],
            "Close": [10.0, 5.2],
            "Volume": [1000.0, 2000.0],
            "Amount": [10000.0, 10400.0],
        }
    )
    factors = pd.DataFrame(
        {"Date": ["2026-03-27", "2026-03-30"], "AdjFactor": [1.0, 2.0]}
    )

    adjusted = bar_utils.apply_hfq_adjustment(bars, factors)

    assert adjusted["Open"].tolist() == [10.0, 10.2]
    assert adjusted["Close"].tolist() == [10.0, 10.4]
    assert adjusted["Volume"].tolist() == [1000.0, 1000.0]
    assert adjusted["Amount"].tolist() == [10000.0, 10400.0]


@pytest.mark.parametrize(
    ("module", "fetch_name", "symbol"),
    [
        (tushare_stock, "fetch_stock_ohlcv", "600519.SH"),
        (tushare_etf, "fetch_etf_ohlcv", "510300.SH"),
    ],
)
def test_a_share_fetch_defaults_to_hfq_prices(
    monkeypatch: pytest.MonkeyPatch, module, fetch_name: str, symbol: str
) -> None:
    """Catch either A-share adapter publishing unadjusted bars by default."""
    raw = pd.DataFrame(
        {
            "Date": ["2026-03-27", "2026-03-30"],
            "Open": [10.0, 5.1],
            "High": [10.2, 5.3],
            "Low": [9.8, 5.0],
            "Close": [10.0, 5.2],
            "Volume": [1000.0, 2000.0],
            "Amount": [10000.0, 10400.0],
        }
    )
    monkeypatch.setattr(
        module,
        "_fetch_tushare_ohlcv" if module is tushare_stock else "_fetch_tushare_etf_ohlcv",
        lambda *args, **kwargs: (raw.copy(), "a_share", symbol),
    )
    monkeypatch.setattr(
        module,
        "_fetch_hfq_factors",
        lambda *args, **kwargs: pd.DataFrame(
            {"Date": ["2026-03-27", "2026-03-30"], "AdjFactor": [1.0, 2.0]}
        ),
        raising=False,
    )

    frame, metadata = getattr(module, fetch_name)(
        symbol, "2026-03-27", "2026-03-30", "daily"
    )

    assert frame["Close"].tolist() == [10.0, 10.4]
    assert metadata["adjustment"] == "hfq"
    assert metadata["adjustment_factor_sha256"]


def test_etf_weekly_is_resampled_after_daily_hfq_adjustment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch applying only the week-ending factor to an already aggregated raw week."""
    daily_raw = pd.DataFrame(
        {
            "Date": ["2026-03-27", "2026-03-30"],
            "Open": [10.0, 5.1],
            "High": [10.2, 5.3],
            "Low": [9.8, 5.0],
            "Close": [10.0, 5.2],
            "Volume": [1000.0, 2000.0],
            "Amount": [10000.0, 10400.0],
        }
    )

    def fake_raw(*args, **kwargs):
        assert kwargs["period"] == "daily"
        return daily_raw.copy(), "a_share", "510300.SH"

    monkeypatch.setattr(tushare_etf, "_fetch_tushare_etf_ohlcv", fake_raw)
    monkeypatch.setattr(
        tushare_etf,
        "_fetch_hfq_factors",
        lambda *args, **kwargs: pd.DataFrame(
            {"Date": ["2026-03-27", "2026-03-30"], "AdjFactor": [1.0, 2.0]}
        ),
        raising=False,
    )

    weekly, _metadata = tushare_etf.fetch_etf_ohlcv(
        "510300.SH", "2026-03-27", "2026-03-30", "weekly"
    )

    assert weekly["Open"].tolist() == [10.0, 10.2]
    assert weekly["Close"].tolist() == [10.0, 10.4]
