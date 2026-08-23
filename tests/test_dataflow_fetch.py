from __future__ import annotations

import pandas as pd
import pytest

import dataflows.tushare_etf as tushare_etf
import dataflows.tushare_stock as tushare_stock


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
    }


def test_fetch_etf_ohlcv_returns_dataframe_and_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tushare_etf,
        "_fetch_tushare_etf_ohlcv",
        lambda *args, **kwargs: (SAMPLE, "a_share", "510300.SH"),
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
